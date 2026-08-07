"""Configuration for the 0DTE short-put strategy backtest."""
from dataclasses import dataclass


@dataclass
class StrategyConfig:
    # ---- Timing (IST) ----
    entry_time_ist: str = "17:00:00"    # daily entry (sell ATM put)
    expiry_time_ist: str = "17:30:00"   # daily cash settlement time
    close_time_ist: str = "17:00:00"    # daily close on expiry day

    # ---- Sizing / accounting ----
    start_balance_usd: float = 1000.0
    target_pct: float = 0.002           # 0.2% of balance per day
    pts_per_usd: int = 1000             # 1 USD = 1000 pts (0.001 BTC/lot, price in pts)
    lot_rounding: str = "ceil"          # ceil | floor | nearest
    min_lots: int = 1

    # ---- Exits ----
    limit_buy_price: float = 10.0       # resting limit buy (profit-taking)
    # carry-forward: next-day budget = 0.2%*bal + buyback_pts*lots
    carry_on_limit_exit: bool = False   # limit exit -> next day FRESH (no carry)
    carry_on_market_exit: bool = True   # market/settle exit -> carry buyback cost

    # ---- Costs ----
    taker_fee: float = 0.0005           # 0.05% on fee basis
    maker_fee: float = 0.0002           # 0.02%
    slippage_pts: float = 0.5           # adverse slippage per fill (pts)
    fee_on: str = "premium"             # 'premium' (standard for crypto opts) | 'notional' (strike-based)

    # ---- Instrument ----
    strike_interval: int = 200          # Delta strike grid
    opt_type: str = "P"                 # always short puts

    # ---- Data range (inclusive) ----
    start_date: str = "2024-04-01"
    end_date: str = "2026-07-25"

    # ---- Outputs ----
    results_dir: str = "backtest/results"
