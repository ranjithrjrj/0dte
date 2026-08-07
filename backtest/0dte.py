"""
0DTE short-ATM-put strategy backtest.

Every day at 17:00 IST: short the ATM put expiring NEXT day (17:30 IST).
  - size = ceil( (0.2% * balance + carry) / entry_price ), round up
  - resting limit BUY @ price 10 (profit-taking; next day FRESH)
  - otherwise close at 17:00 IST on expiry day (market) -> next-day budget
    adds (buyback_price * lots) as carry
  - balance compounds with realized P&L; cash-settled at expiry (intrinsic
    fallback if no trade prints near close)

Usage:
    python backtest/0dte.py            # run full backtest
"""
import argparse
import json
import math
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import StrategyConfig

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:admin@localhost:5432/delta")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def at_ist(d: date, hm: str) -> datetime:
    """Return aware UTC datetime for the given IST clock time on date d."""
    hh, mm, ss = map(int, hm.split(":"))
    dt = datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST)
    return dt.astimezone(UTC)


# ---------------- DB helpers ----------------

def last_futures_price(cur, ts):
    cur.execute("SELECT price FROM futures_trades WHERE ts <= %s ORDER BY ts DESC LIMIT 1", (ts,))
    r = cur.fetchone()
    return float(r[0]) if r else None


def last_option_trade(cur, opt_type, strike, expiry, ts):
    cur.execute(
        "SELECT price FROM options_trades "
        "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts<=%s "
        "ORDER BY ts DESC LIMIT 1", (opt_type, strike, expiry, ts))
    r = cur.fetchone()
    return float(r[0]) if r else None


def last_option_trade_ts(cur, opt_type, strike, expiry, ts):
    cur.execute(
        "SELECT ts FROM options_trades "
        "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts<=%s "
        "ORDER BY ts DESC LIMIT 1", (opt_type, strike, expiry, ts))
    r = cur.fetchone()
    return r[0] if r else None


def min_option_price(cur, opt_type, strike, expiry, ts_lo, ts_hi):
    cur.execute(
        "SELECT min(price) FROM options_trades "
        "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts>=%s AND ts<=%s",
        (opt_type, strike, expiry, ts_lo, ts_hi))
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else None


def first_limit_trade_ts(cur, opt_type, strike, expiry, ts_lo, ts_hi, limit):
    cur.execute(
        "SELECT min(ts) FROM options_trades "
        "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts>=%s AND ts<=%s AND price<=%s",
        (opt_type, strike, expiry, ts_lo, ts_hi, limit))
    r = cur.fetchone()
    return r[0] if r and r[0] is not None else None


# ---------------- Engine ----------------

def run(cfg: StrategyConfig, verbose=True):
    results_dir = Path(cfg.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    start = date.fromisoformat(cfg.start_date)
    end = date.fromisoformat(cfg.end_date)

    balance = cfg.start_balance_usd
    carry_pts = 0.0
    trades = []
    daily = {}          # date -> balance after that day's close
    skipped = {"no_futures": 0, "no_entry_trade": 0, "entry_le_limit": 0, "no_close": 0}

    def log(msg):
        if verbose:
            print(msg)

    with psycopg.connect(DATABASE_URL) as conn:
        cur = conn.cursor()
        d = start
        while d <= end:
            expiry = d + timedelta(days=1)
            entry_ts = at_ist(d, cfg.entry_time_ist)
            close_ts = at_ist(expiry, cfg.close_time_ist)
            expiry_ts = at_ist(expiry, cfg.expiry_time_ist)

            fut = last_futures_price(cur, entry_ts)
            if fut is None:
                skipped["no_futures"] += 1
                daily[d.isoformat()] = balance
                d += timedelta(days=1)
                continue

            strike = int(round(fut / cfg.strike_interval)) * cfg.strike_interval
            entry_px = last_option_trade(cur, cfg.opt_type, strike, expiry, entry_ts)

            if entry_px is None:
                skipped["no_entry_trade"] += 1
                daily[d.isoformat()] = balance
                d += timedelta(days=1)
                continue
            if entry_px <= cfg.limit_buy_price:
                skipped["entry_le_limit"] += 1
                daily[d.isoformat()] = balance
                d += timedelta(days=1)
                continue

            # ---- sizing (target pts = 0.2% * balance + carry) ----
            target_pts = cfg.target_pct * balance * cfg.pts_per_usd + carry_pts
            lots = target_pts / entry_px
            lots = math.ceil(lots) if cfg.lot_rounding == "ceil" else \
                   math.floor(lots) if cfg.lot_rounding == "floor" else int(round(lots))
            lots = max(lots, cfg.min_lots)

            # entry fill (short -> receive slightly less due to slippage)
            entry_fill = entry_px - cfg.slippage_pts

            # ---- exits ----
            min_px = min_option_price(cur, cfg.opt_type, strike, expiry, entry_ts, close_ts)
            if min_px is not None and min_px <= cfg.limit_buy_price:
                exit_type = "limit"
                exit_px = cfg.limit_buy_price
                exit_fill = cfg.limit_buy_price
                exit_ts = first_limit_trade_ts(cur, cfg.opt_type, strike, expiry,
                                               entry_ts, close_ts, cfg.limit_buy_price)
            else:
                exit_px = last_option_trade(cur, cfg.opt_type, strike, expiry, close_ts)
                if exit_px is None:
                    fut_exp = last_futures_price(cur, expiry_ts)
                    if fut_exp is None:
                        skipped["no_close"] += 1
                        daily[d.isoformat()] = balance
                        d += timedelta(days=1)
                        continue
                    exit_px = max(strike - fut_exp, 0.0)
                    exit_type = "settle"
                    exit_ts = expiry_ts
                else:
                    exit_type = "market"
                    exit_ts = last_option_trade_ts(cur, cfg.opt_type, strike, expiry, close_ts)
                exit_fill = exit_px + cfg.slippage_pts  # buy back -> pay more

            # ---- P&L ----
            pnl_pts = (entry_fill - exit_fill) * lots
            pnl_usd = pnl_pts * 0.001
            entry_fee_base = entry_fill * lots * 0.001 if cfg.fee_on == "premium" else strike * lots * 0.001
            exit_fee_base = exit_fill * lots * 0.001 if cfg.fee_on == "premium" else strike * lots * 0.001
            exit_fee_rate = cfg.maker_fee if exit_type == "limit" else cfg.taker_fee
            fees_usd = entry_fee_base * cfg.taker_fee + exit_fee_base * exit_fee_rate
            net = pnl_usd - fees_usd
            balance += net

            # ---- carry-forward ----
            if exit_type == "limit" and not cfg.carry_on_limit_exit:
                carry_pts = 0.0
            else:
                carry_pts = exit_fill * lots

            trades.append({
                "date": d.isoformat(),
                "expiry": expiry.isoformat(),
                "strike": strike,
                "futures_at_entry": round(fut, 2),
                "entry_px": round(entry_px, 2),
                "entry_fill": round(entry_fill, 2),
                "lots": lots,
                "target_pts": round(target_pts, 2),
                "exit_type": exit_type,
                "exit_px": round(exit_px, 2),
                "exit_fill": round(exit_fill, 2),
                "exit_ts_utc": exit_ts.isoformat() if exit_ts else None,
                "pnl_pts": round(pnl_pts, 2),
                "pnl_usd": round(pnl_usd, 2),
                "fees_usd": round(fees_usd, 2),
                "net_usd": round(net, 2),
                "balance": round(balance, 2),
            })
            daily[d.isoformat()] = round(balance, 2)
            d += timedelta(days=1)

    # ---- full daily equity (carry balance on skipped days) ----
    equity = []
    bal = cfg.start_balance_usd
    for i in range((end - start).days + 1):
        dd = (start + timedelta(days=i)).isoformat()
        bal = daily.get(dd, bal)
        equity.append({"date": dd, "balance": round(bal, 2)})

    write_results(cfg, trades, equity, skipped)
    return trades, equity, skipped


def write_results(cfg, trades, equity, skipped):
    results_dir = Path(cfg.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    # trades CSV
    import csv as _csv
    with open(results_dir / "trades.csv", "w", newline="", encoding="utf-8") as f:
        if trades:
            w = _csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)

    # equity CSV
    with open(results_dir / "equity.csv", "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["date", "balance"])
        w.writeheader()
        w.writerows(equity)

    # summary
    summary = summarize(cfg, trades, equity, skipped)
    with open(results_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print("\nSummary written to", results_dir)
    return summary


def summarize(cfg, trades, equity, skipped):
    n = len(trades)
    wins = [t for t in trades if t["net_usd"] > 0]
    losses = [t for t in trades if t["net_usd"] < 0]
    gross_profit = sum(t["net_usd"] for t in wins)
    gross_loss = -sum(t["net_usd"] for t in losses)
    end_balance = equity[-1]["balance"] if equity else cfg.start_balance_usd
    total_pnl = end_balance - cfg.start_balance_usd
    return_pct = 100.0 * total_pnl / cfg.start_balance_usd

    # max drawdown on equity
    peak = cfg.start_balance_usd
    max_dd = 0.0
    max_dd_pct = 0.0
    for e in equity:
        peak = max(peak, e["balance"])
        dd = peak - e["balance"]
        if dd > max_dd:
            max_dd = dd
        if peak > 0:
            dd_pct = 100.0 * dd / peak
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

    n_days = len(equity)
    years = n_days / 365.25
    cagr = (100.0 * ((end_balance / cfg.start_balance_usd) ** (1 / years) - 1)) if years > 0 and end_balance > 0 else 0.0

    return {
        "start_balance": cfg.start_balance_usd,
        "end_balance": round(end_balance, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "total_return_pct": round(return_pct, 2),
        "cagr_pct": round(cagr, 2),
        "n_trades": n,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / n, 2) if n else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_net_usd": round(total_pnl / n, 2) if n else 0.0,
        "total_fees_usd": round(sum(t["fees_usd"] for t in trades), 2),
        "gross_profit_usd": round(gross_profit, 2),
        "gross_loss_usd": round(gross_loss, 2),
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "best_day_usd": round(max((t["net_usd"] for t in trades), default=0), 2),
        "worst_day_usd": round(min((t["net_usd"] for t in trades), default=0), 2),
        "avg_lots": round(sum(t["lots"] for t in trades) / n, 2) if n else 0.0,
        "exit_breakdown": {
            "limit": sum(1 for t in trades if t["exit_type"] == "limit"),
            "market": sum(1 for t in trades if t["exit_type"] == "market"),
            "settle": sum(1 for t in trades if t["exit_type"] == "settle"),
        },
        "days_skipped": skipped,
        "date_from": equity[0]["date"] if equity else None,
        "date_to": equity[-1]["date"] if equity else None,
        "params": {k: v for k, v in cfg.__dict__.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="override start date YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="override end date YYYY-MM-DD")
    args = ap.parse_args()
    cfg = StrategyConfig()
    if args.start:
        cfg.start_date = args.start
    if args.end:
        cfg.end_date = args.end

    print("=" * 70)
    print("0DTE SHORT-ATM-PUT BACKTEST")
    print(f"range: {cfg.start_date} -> {cfg.end_date}  (entry 17:00 IST, close 17:00 IST next day)")
    print(f"target: 0.2%/day | limit buy @ {cfg.limit_buy_price} | fees t{cfg.taker_fee}/m{cfg.maker_fee} | slip {cfg.slippage_pts}")
    print("=" * 70)

    trades, equity, skipped = run(cfg, verbose=True)

    summary = summarize(cfg, trades, equity, skipped)
    print("\n===== SUMMARY =====")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
