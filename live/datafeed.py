"""Data-feed abstraction for the live paper-trading engine.

The r3 paper engine is written against this interface, so the exact same code
runs on:
  - ReplayFeed : reads TimescaleDB (used to validate the engine vs the backtest)
  - DeltaFeed  : live Delta Exchange data (completed once API facts confirmed)
"""
from abc import ABC, abstractmethod
from datetime import date, datetime


class DataFeed(ABC):
    @abstractmethod
    def futures_price_at(self, ts: datetime):
        """Last BTCUSD perpetual price at/before ts (or None)."""

    @abstractmethod
    def option_price_at(self, opt_type: str, strike, expiry: date, ts: datetime):
        """Last option traded price at/before ts (entry/close reference; or None)."""

    @abstractmethod
    def option_trades(self, opt_type: str, strike, expiry: date, ts_from: datetime, ts_to: datetime):
        """List of (ts, price) trades for the option in [ts_from, ts_to], ascending."""

    def product_symbol(self, opt_type: str, strike, expiry: date) -> str:
        """Delta product symbol, e.g. P-BTC-72800-010424."""
        return f"{opt_type}-BTC-{strike}-{expiry:%d%m%y}"
