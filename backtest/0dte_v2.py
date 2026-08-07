"""
Strategy #2: "Rolling Premium Budget" short-ATM-put.

Same mechanics as strategy #1 (daily 17:00 IST short ATM put expiring next day,
limit-buy @10 profit-take, 17:00 next-day market close, cash-settle fallback,
fees + slippage, 7 days/week) BUT position sizing differs:

  - Fresh budget  = 1% of account balance (in points). e.g. $1000 -> 10000 pts.
  - Rolling       : while a position does NOT close at the limit-10, the next
                    day's budget = current position buy-back value (exit_px*lots).
  - Floor         : budget never drops below 1% of the (compounded) balance.
  - Reset         : only a limit-10 close resets to fresh 1% of current balance.

Usage:
    python backtest/0dte_v2.py
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
from config import StrategyConfig  # noqa: E402  (reused for base fields)
from d0te_helpers import (at_ist, last_futures_price, last_option_trade,  # noqa: E402
                          last_option_trade_ts, min_option_price,
                          first_limit_trade_ts, write_results, summarize)

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:admin@localhost:5432/delta")


@dataclass
class V2Config:
    entry_time_ist: str = "17:00:00"
    expiry_time_ist: str = "17:30:00"
    close_time_ist: str = "17:00:00"
    start_balance_usd: float = 1000.0
    target_pct: float = 0.01            # 1% fresh budget
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
    results_dir: str = "backtest/results_v2"


def run(cfg: V2Config, verbose=True):
    results_dir = Path(cfg.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    start = date.fromisoformat(cfg.start_date)
    end = date.fromisoformat(cfg.end_date)

    balance = cfg.start_balance_usd
    budget_pts = cfg.target_pct * balance * cfg.pts_per_usd   # fresh 1%
    trades = []
    daily = {}
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

            # ---- sizing from rolling budget (with 1% floor already applied) ----
            lots = budget_pts / entry_px
            lots = math.ceil(lots) if cfg.lot_rounding == "ceil" else \
                   math.floor(lots) if cfg.lot_rounding == "floor" else int(round(lots))
            lots = max(lots, cfg.min_lots)
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

            # ---- next budget (rolling with 1% floor) ----
            fresh = cfg.target_pct * balance * cfg.pts_per_usd
            if exit_type == "limit":
                budget_pts = fresh
            else:
                budget_pts = max(exit_fill * lots, fresh)   # floor at 1% of balance

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
            d += timedelta(days=1)

    equity = []
    bal = cfg.start_balance_usd
    for i in range((end - start).days + 1):
        dd = (start + timedelta(days=i)).isoformat()
        bal = daily.get(dd, bal)
        equity.append({"date": dd, "balance": round(bal, 2)})

    summary = write_results(cfg, trades, equity, skipped)
    if verbose:
        print("\n===== SUMMARY (Strategy #2) =====")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    return trades, equity, skipped, summary


def main():
    cfg = V2Config()
    print("=" * 70)
    print("STRATEGY #2 — 1% ROLLING PREMIUM BUDGET (short ATM put, 0DTE)")
    print(f"range: {cfg.start_date} -> {cfg.end_date} | fresh 1% budget | limit @ {cfg.limit_buy_price} | floor 1%")
    print("=" * 70)
    run(cfg, verbose=True)


if __name__ == "__main__":
    main()
