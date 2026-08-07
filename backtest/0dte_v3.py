"""
Strategy #3: Strategy #2 (1% rolling budget) + RISK GUARDS.

Adds to the 1% rolling-budget short-put:
  - BUDGET CAP : next-day budget clamped to at most `budget_cap_pct` of balance
                 (default 5%).  budget = clamp(rolling, 1%*bal, 5%*bal)
  - STOP-LOSS  : if the put's traded price reaches `stop_loss_mult` * entry
                 (default 2x -> a ~100% loss of that day's premium), close at
                 the stop level immediately (taker fill).
  - LIQUIDATION GUARD : if balance would go <= 0, the account is blown -> halt.

Usage:
    python backtest/0dte_v3.py
"""
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from d0te_helpers import (at_ist, last_futures_price, last_option_trade,
                          last_option_trade_ts, first_limit_trade_ts,
                          first_trade_ts_price_ge, write_results)

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:admin@localhost:5432/delta")


@dataclass
class V3Config:
    entry_time_ist: str = "17:00:00"
    expiry_time_ist: str = "17:30:00"
    close_time_ist: str = "17:00:00"
    start_balance_usd: float = 1000.0
    target_pct: float = 0.01            # fresh 1%
    floor_pct: float = 0.01             # floor budget = this % of balance
    budget_cap_pct: float = 0.05        # never budget more than 5% of balance
    stop_loss_mult: float = 2.0         # close if put premium >= 2x entry
    # --- drawdown refinements ---
    post_stop_reset: bool = False       # after a stop, reset budget to floor (break amplification)
    loss_throttle_n: int = 0            # halve budget after N consecutive losses (0 = off)
    loss_throttle_factor: float = 0.5
    post_stop_skip: bool = False        # skip next day after a stop-out (cooldown)
    ivp_min: float = 0.0                # only trade when IVP > this (0 = off)
    ivp_max: float = 100.0              # only trade when IVP < this
    pts_per_usd: int = 1000
    lot_rounding: str = "ceil"
    min_lots: int = 1
    limit_buy_price: float = 10.0
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    slippage_pts: float = 0.5
    fee_on: str = "premium"
    strike_interval: int = 200
    opt_type: str = "P"
    start_date: str = "2024-04-01"
    end_date: str = "2026-07-25"
    results_dir: str = "backtest/results_v3"


def run(cfg: V3Config, verbose=True):
    results_dir = Path(cfg.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    start = date.fromisoformat(cfg.start_date)
    end = date.fromisoformat(cfg.end_date)

    balance = cfg.start_balance_usd
    budget_pts = cfg.target_pct * balance * cfg.pts_per_usd
    trades = []
    daily = {}
    skipped = {"no_futures": 0, "no_entry_trade": 0, "entry_le_limit": 0, "no_close": 0,
               "ivp_gate": 0}
    blown = None
    consec_losses = 0
    skip_stop = False

    def log(msg):
        if verbose:
            print(msg)

    # IVP series (trailing-365d IV percentile) from analysis/iv_series.py output
    ivp_map = {}
    if cfg.ivp_min > 0 or cfg.ivp_max < 100:
        ivp_csv = Path("data/iv_series.csv")
        if ivp_csv.exists():
            import pandas as pd
            ivp_df = pd.read_csv(ivp_csv)
            ivp_map = dict(zip(ivp_df["date"].astype(str),
                               pd.to_numeric(ivp_df["ivp"], errors="coerce")))
            log(f"  IVP gate enabled ({cfg.ivp_min:g}<IVP<{cfg.ivp_max:g}), "
                f"{len(ivp_map)} daily IVP points loaded")
        else:
            log("  !! data/iv_series.csv not found; IVP gate will skip all days")

    with psycopg.connect(DATABASE_URL) as conn:
        cur = conn.cursor()
        d = start
        while d <= end:
            if blown is not None:               # account dead
                daily[d.isoformat()] = round(balance, 2)
                d += timedelta(days=1)
                continue

            if skip_stop:                       # cooldown after a stop-out
                skip_stop = False
                daily[d.isoformat()] = round(balance, 2)
                d += timedelta(days=1)
                continue

            if cfg.ivp_min > 0 or cfg.ivp_max < 100:
                ivp = ivp_map.get(d.isoformat())
                if ivp is None or ivp <= cfg.ivp_min or ivp >= cfg.ivp_max:
                    skipped["ivp_gate"] += 1
                    daily[d.isoformat()] = round(balance, 2)
                    d += timedelta(days=1)
                    continue

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

            eff_budget = budget_pts
            if cfg.loss_throttle_n and consec_losses >= cfg.loss_throttle_n:
                eff_budget *= cfg.loss_throttle_factor
            lots = eff_budget / entry_px
            lots = math.ceil(lots) if cfg.lot_rounding == "ceil" else \
                   math.floor(lots) if cfg.lot_rounding == "floor" else int(round(lots))
            lots = max(lots, cfg.min_lots)
            entry_fill = entry_px - cfg.slippage_pts

            # ---- exits: limit(10) vs stop(2x entry) vs market/close ----
            stop_px = cfg.stop_loss_mult * entry_px
            limit_ts = first_limit_trade_ts(cur, cfg.opt_type, strike, expiry,
                                            entry_ts, close_ts, cfg.limit_buy_price)
            stop_ts = first_trade_ts_price_ge(cur, cfg.opt_type, strike, expiry,
                                              entry_ts, close_ts, stop_px)

            if limit_ts is not None and (stop_ts is None or limit_ts <= stop_ts):
                exit_type, exit_px, exit_fill, exit_ts = "limit", cfg.limit_buy_price, cfg.limit_buy_price, limit_ts
            elif stop_ts is not None:
                exit_type, exit_px, exit_fill, exit_ts = "stop", stop_px, stop_px, stop_ts
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
                    exit_type, exit_ts = "settle", expiry_ts
                else:
                    exit_type = "market"
                    exit_ts = last_option_trade_ts(cur, cfg.opt_type, strike, expiry, close_ts)
                exit_fill = exit_px + cfg.slippage_pts

            # ---- P&L ----
            pnl_pts = (entry_fill - exit_fill) * lots
            pnl_usd = pnl_pts * 0.001
            entry_fee_base = entry_fill * lots * 0.001 if cfg.fee_on == "premium" else strike * lots * 0.001
            exit_fee_base = exit_fill * lots * 0.001 if cfg.fee_on == "premium" else strike * lots * 0.001
            exit_fee_rate = cfg.maker_fee if exit_type == "limit" else cfg.taker_fee
            fees_usd = entry_fee_base * cfg.taker_fee + exit_fee_base * exit_fee_rate
            net = pnl_usd - fees_usd
            balance += net
            consec_losses = consec_losses + 1 if net < 0 else 0

            # ---- next budget: fresh on limit/stop-reset, else clamp(rolling, floor, cap) ----
            fresh = cfg.floor_pct * max(balance, 0.0) * cfg.pts_per_usd
            cap = cfg.budget_cap_pct * max(balance, 0.0) * cfg.pts_per_usd
            if exit_type == "limit" or (exit_type == "stop" and cfg.post_stop_reset):
                budget_pts = fresh
            else:
                budget_pts = max(fresh, min(exit_fill * lots, cap))
            if exit_type == "stop" and cfg.post_stop_skip:
                skip_stop = True

            trades.append({
                "date": d.isoformat(),
                "expiry": expiry.isoformat(),
                "strike": strike,
                "futures_at_entry": round(fut, 2),
                "entry_px": round(entry_px, 2),
                "entry_fill": round(entry_fill, 2),
                "lots": lots,
                "budget_pts": round(budget_pts, 2),
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

            if balance <= 0:
                blown = d.isoformat()
                balance = 0.0
                log(f"  !! ACCOUNT BLOWN on {blown} (balance <= 0); halting.")
            d += timedelta(days=1)

    equity = []
    bal = cfg.start_balance_usd
    for i in range((end - start).days + 1):
        dd = (start + timedelta(days=i)).isoformat()
        bal = daily.get(dd, bal)
        equity.append({"date": dd, "balance": round(bal, 2)})

    summary = write_results(cfg, trades, equity, skipped)
    summary["blown"] = blown is not None
    summary["blown_date"] = blown
    if verbose:
        print("\n===== SUMMARY (Strategy #3 — capped 5% + stop 2x) =====")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    return trades, equity, skipped, summary


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", type=float, default=2.0, help="stop-loss multiple of entry premium")
    ap.add_argument("--cap", type=float, default=0.05, help="budget cap as fraction of balance")
    ap.add_argument("--floor", type=float, default=0.01, help="budget floor as fraction of balance")
    ap.add_argument("--post_stop_reset", action="store_true", help="reset budget to floor after a stop")
    ap.add_argument("--loss_throttle", type=int, default=0, help="halve budget after N consecutive losses (0=off)")
    ap.add_argument("--post_stop_skip", action="store_true", help="skip next day after a stop-out")
    ap.add_argument("--ivp", type=float, default=0.0, help="only trade when IVP > this (0=off)")
    ap.add_argument("--ivp-max", type=float, default=100.0, help="only trade when IVP < this")
    ap.add_argument("--start", default=None, help="override start date YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="override end date YYYY-MM-DD")
    ap.add_argument("--results", default=None, help="override results dir")
    args = ap.parse_args()
    cfg = V3Config(
        stop_loss_mult=args.stop,
        budget_cap_pct=args.cap,
        floor_pct=args.floor,
        post_stop_reset=args.post_stop_reset,
        loss_throttle_n=args.loss_throttle,
        post_stop_skip=args.post_stop_skip,
        ivp_min=args.ivp,
        ivp_max=args.ivp_max,
        start_date=args.start or V3Config().start_date,
        end_date=args.end or V3Config().end_date,
        results_dir=args.results
        or f"backtest/results_sweep/{args.stop:g}x_{int(round(args.cap * 100))}pct"
        + (f"_ivp{int(args.ivp)}" if args.ivp > 0 else ""),
    )
    print("=" * 70)
    print(f"STRATEGY #3 — 1% ROLLING + CAP {cfg.budget_cap_pct*100:.0f}% + STOP {cfg.stop_loss_mult:g}x"
          f" | floor {cfg.floor_pct*100:.0f}%"
          f" | reset={cfg.post_stop_reset} throttle={cfg.loss_throttle_n} skip={cfg.post_stop_skip}")
    print(f"range: {cfg.start_date} -> {cfg.end_date} | limit @10 | results -> {cfg.results_dir}")
    print("=" * 70)
    run(cfg, verbose=True)


if __name__ == "__main__":
    main()
