"""r3 paper engine — stateful, driven by a DataFeed.

Mirrors the confirmed r3 rules exactly:
  - daily 17:00 IST short of the ATM put expiring next day (17:30 IST cash-settle)
  - 1% rolling budget, floor 1%, cap 5%, round-up lots, compounding
  - resting limit buy @10 (fills on first print <=10) OR 2x stop (first print >= 2*entry)
  - 17:00 next-day market close; cash-settle fallback if no print
  - skip the next trading day after a stop-out (r3's key rule)

Two usage modes:
  - replay : step(date) reads the full day's window from the feed at once
             (validated against the r3 backtest).
  - live   : open_position(date) at 17:00, poll(now) repeatedly for limit/stop
             triggers, close_position() at 17:00 next day.
"""
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PTS_PER_USD = 1000  # 1 lot = 0.001 BTC; USD = pts * 0.001


def at_ist(d: date, hm: str) -> datetime:
    hh, mm, ss = map(int, hm.split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST).astimezone(UTC)


@dataclass
class PaperConfig:
    entry_time_ist: str = "17:00:00"
    expiry_time_ist: str = "17:30:00"
    close_time_ist: str = "17:00:00"
    start_balance_usd: float = 1000.0
    target_pct: float = 0.01     # fresh 1%
    floor_pct: float = 0.01      # floor 1%
    cap_pct: float = 0.05        # cap 5%
    stop_mult: float = 2.0       # 2x stop
    limit_price: float = 10.0    # limit buy @10
    slippage: float = 0.5        # pts
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    fee_on: str = "premium"
    strike_interval: int = 200
    lot_rounding: str = "ceil"
    min_lots: int = 1
    opt_type: str = "P"
    skip_after_stop: bool = True   # r3 rule
    post_stop_reset: bool = False


class PaperEngine:
    def __init__(self, cfg: PaperConfig, feed):
        self.cfg = cfg
        self.feed = feed
        self.balance = cfg.start_balance_usd
        self.budget_pts = cfg.target_pct * cfg.start_balance_usd * PTS_PER_USD
        self.skip_next = False
        self.consec_losses = 0
        self.position = None   # live open position
        self._window = []      # accumulated trades for the live position

    # ---------------- state persistence ----------------
    def state(self) -> dict:
        return {
            "balance": self.balance,
            "budget_pts": self.budget_pts,
            "skip_next": self.skip_next,
            "consec_losses": self.consec_losses,
            "position": self._position_to_state(),
        }

    def _position_to_state(self):
        if self.position is None:
            return None
        p = self.position
        return {
            "d": p["d"].isoformat(), "expiry": p["expiry"].isoformat(),
            "expiry_ts": p["expiry_ts"].isoformat(), "close_ts": p["close_ts"].isoformat(),
            "last_check": p["last_check"].isoformat(),
            "strike": p["strike"], "entry_px": p["entry_px"], "entry_fill": p["entry_fill"],
            "lots": p["lots"], "fut": p["fut"], "symbol": p["symbol"],
        }

    def _position_from_state(self, s):
        if not s:
            return None
        def _p(x):
            return datetime.fromisoformat(x)
        return {
            "d": date.fromisoformat(s["d"]), "expiry": date.fromisoformat(s["expiry"]),
            "expiry_ts": _p(s["expiry_ts"]), "close_ts": _p(s["close_ts"]),
            "last_check": _p(s["last_check"]),
            "strike": s["strike"], "entry_px": s["entry_px"], "entry_fill": s["entry_fill"],
            "lots": s["lots"], "fut": s["fut"], "symbol": s["symbol"],
        }

    def load_state(self, s: dict):
        if not s:
            return
        self.balance = float(s.get("balance", self.balance))
        self.budget_pts = float(s.get("budget_pts", self.budget_pts))
        self.skip_next = bool(s.get("skip_next", False))
        self.consec_losses = int(s.get("consec_losses", 0))
        self.position = self._position_from_state(s.get("position"))

    # ---------------- shared exit finalization ----------------
    def _finalize(self, d, expiry, strike, fut, entry_px, entry_fill, lots,
                  exit_type, exit_px, exit_fill, exit_ts):
        cfg = self.cfg
        pnl_pts = (entry_fill - exit_fill) * lots
        pnl_usd = pnl_pts * 0.001
        entry_base = entry_fill * lots * 0.001 if cfg.fee_on == "premium" else strike * lots * 0.001
        exit_base = exit_fill * lots * 0.001 if cfg.fee_on == "premium" else strike * lots * 0.001
        fee_rate = cfg.maker_fee if exit_type == "limit" else cfg.taker_fee
        fees = entry_base * cfg.taker_fee + exit_base * fee_rate
        net = pnl_usd - fees
        self.balance += net
        self.consec_losses = self.consec_losses + 1 if net < 0 else 0

        fresh = cfg.floor_pct * max(self.balance, 0.0) * PTS_PER_USD
        cap = cfg.cap_pct * max(self.balance, 0.0) * PTS_PER_USD
        if exit_type == "limit" or (exit_type == "stop" and cfg.post_stop_reset):
            self.budget_pts = fresh
        else:
            self.budget_pts = max(fresh, min(exit_fill * lots, cap))
        if exit_type == "stop" and cfg.skip_after_stop:
            self.skip_next = True

        return {
            "date": d.isoformat(), "expiry": expiry.isoformat(), "strike": strike,
            "futures_at_entry": round(fut, 2), "entry_px": round(entry_px, 2),
            "entry_fill": round(entry_fill, 2), "lots": lots,
            "budget_pts": round(self.budget_pts, 2),
            "exit_type": exit_type, "exit_px": round(exit_px, 2),
            "exit_fill": round(exit_fill, 2),
            "exit_ts_utc": exit_ts.isoformat() if exit_ts else None,
            "pnl_pts": round(pnl_pts, 2), "pnl_usd": round(pnl_usd, 2),
            "fees_usd": round(fees, 2), "net_usd": round(net, 2),
            "balance": round(self.balance, 2),
        }

    def _entry(self, d):
        """Common entry: returns (skip_dict_or_None, ctx_or_None)."""
        cfg = self.cfg
        expiry = d + timedelta(days=1)
        entry_ts = at_ist(d, cfg.entry_time_ist)
        close_ts = at_ist(expiry, cfg.close_time_ist)
        expiry_ts = at_ist(expiry, cfg.expiry_time_ist)
        fut = self.feed.futures_price_at(entry_ts)
        if fut is None:
            return {"date": d.isoformat(), "skip": True, "reason": "no futures"}, None
        strike = int(round(fut / cfg.strike_interval)) * cfg.strike_interval
        entry_px = self.feed.option_price_at(cfg.opt_type, strike, expiry, entry_ts)
        if entry_px is None:
            return {"date": d.isoformat(), "skip": True, "reason": "no entry trade"}, None
        if entry_px <= cfg.limit_price:
            return {"date": d.isoformat(), "skip": True, "reason": "entry le limit"}, None
        lots = self.budget_pts / entry_px
        lots = math.ceil(lots) if cfg.lot_rounding == "ceil" else max(1, int(self.budget_pts / entry_px))
        lots = max(lots, cfg.min_lots)
        entry_fill = entry_px - cfg.slippage
        ctx = {"d": d, "expiry": expiry, "entry_ts": entry_ts, "close_ts": close_ts,
               "expiry_ts": expiry_ts, "strike": strike, "fut": fut,
               "entry_px": entry_px, "entry_fill": entry_fill, "lots": lots}
        return None, ctx

    # ---------------- replay: full day at once ----------------
    def step(self, d: date) -> dict:
        cfg = self.cfg
        if self.skip_next:
            self.skip_next = False
            return {"date": d.isoformat(), "skip": True, "reason": "post-stop cooldown"}
        skip, ctx = self._entry(d)
        if skip:
            return skip
        strike, expiry = ctx["strike"], ctx["expiry"]
        entry_ts, close_ts, expiry_ts = ctx["entry_ts"], ctx["close_ts"], ctx["expiry_ts"]
        entry_fill, lots = ctx["entry_fill"], ctx["lots"]
        stop_px = cfg.stop_mult * ctx["entry_px"]

        window = self.feed.option_trades(cfg.opt_type, strike, expiry, entry_ts, close_ts)
        limit_tr = next(((t, p) for t, p in window if p <= cfg.limit_price), None)
        stop_tr = next(((t, p) for t, p in window if p >= stop_px), None)

        if limit_tr is not None and (stop_tr is None or limit_tr[0] <= stop_tr[0]):
            exit_type, exit_px, exit_fill, exit_ts = "limit", cfg.limit_price, cfg.limit_price, limit_tr[0]
        elif stop_tr is not None:
            exit_type, exit_px, exit_fill, exit_ts = "stop", stop_px, stop_px, stop_tr[0]
        elif window:
            exit_type, exit_px, exit_ts = "market", window[-1][1], None
            exit_fill = exit_px + cfg.slippage
        else:
            fut_exp = self.feed.futures_price_at(expiry_ts)
            if fut_exp is None:
                return {"date": d.isoformat(), "skip": True, "reason": "no close"}
            exit_type, exit_px, exit_ts = "settle", max(strike - fut_exp, 0.0), expiry_ts
            exit_fill = exit_px + cfg.slippage

        return self._finalize(d, expiry, strike, ctx["fut"], ctx["entry_px"], entry_fill,
                              lots, exit_type, exit_px, exit_fill, exit_ts)

    # ---------------- live ----------------
    def open_position(self, d: date) -> dict:
        if self.skip_next:
            self.skip_next = False
            return {"date": d.isoformat(), "skip": True, "reason": "post-stop cooldown"}
        skip, ctx = self._entry(d)
        if skip:
            return skip
        symbol = self.feed.product_symbol(self.cfg.opt_type, ctx["strike"], ctx["expiry"])
        self.position = {**ctx, "symbol": symbol, "last_check": ctx["entry_ts"]}
        self._window = []
        return {"date": d.isoformat(), "skip": False, "symbol": symbol,
                "entry_px": round(ctx["entry_px"], 2), "lots": ctx["lots"],
                "futures_at_entry": round(ctx["fut"], 2)}

    def poll(self, now: datetime) -> dict:
        """Check for limit/stop triggers on new trades; returns a final record or None."""
        cfg = self.cfg
        if self.position is None:
            return None
        p = self.position
        new = self.feed.option_trades(cfg.opt_type, p["strike"], p["expiry"],
                                      p["last_check"], now)
        self._window.extend(new)
        p["last_check"] = now
        stop_px = cfg.stop_mult * p["entry_px"]
        limit_tr = next(((t, pr) for t, pr in self._window if pr <= cfg.limit_price), None)
        stop_tr = next(((t, pr) for t, pr in self._window if pr >= stop_px), None)
        if limit_tr is not None and (stop_tr is None or limit_tr[0] <= stop_tr[0]):
            rec = self._finalize(p["d"], p["expiry"], p["strike"], p["fut"], p["entry_px"],
                                 p["entry_fill"], p["lots"], "limit", cfg.limit_price,
                                 cfg.limit_price, limit_tr[0])
            self.position, self._window = None, []
            return rec
        if stop_tr is not None:
            rec = self._finalize(p["d"], p["expiry"], p["strike"], p["fut"], p["entry_px"],
                                 p["entry_fill"], p["lots"], "stop", stop_px, stop_px, stop_tr[0])
            self.position, self._window = None, []
            return rec
        return None

    def close_position(self) -> dict:
        cfg = self.cfg
        if self.position is None:
            return None
        p = self.position
        exit_px = self.feed.option_price_at(cfg.opt_type, p["strike"], p["expiry"], p["close_ts"])
        if exit_px is None:
            fut = self.feed.futures_price_at(p["expiry_ts"])
            exit_px = max(p["strike"] - fut, 0.0)
            exit_type, exit_ts = "settle", p["expiry_ts"]
        else:
            exit_type, exit_ts = "market", p["close_ts"]
        rec = self._finalize(p["d"], p["expiry"], p["strike"], p["fut"], p["entry_px"],
                             p["entry_fill"], p["lots"], exit_type, exit_px,
                             exit_px + cfg.slippage, exit_ts)
        self.position, self._window = None, []
        return rec
