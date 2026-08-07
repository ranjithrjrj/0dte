"""Delta Exchange India live data feed (read-only, public endpoints).

Confirmed from docs.delta.exchange (2026-08-07):
  - Production India REST base: https://api.india.delta.exchange
    (the global https://api.delta.exchange is a DIFFERENT venue)
  - Market data (products / tickers / trades) requires NO authentication.
  - Ticker : GET /v2/tickers/{symbol}  -> result.close  = last traded price
  - Trades : GET /v2/trades/{symbol}   -> result.trades = [{side,size,price,timestamp}]
             timestamps are epoch MICROSECONDS (guard handles seconds too).
  - Option symbol format matches our data: P-BTC-72800-010424

Note: futures_price_at / option_price_at return the CURRENT last-traded price,
which is what the live engine wants at the moment it acts (17:00 entry, etc.).
"""
import os
from datetime import datetime, timezone

import requests

from datafeed import DataFeed

DEFAULT_BASE = "https://api.india.delta.exchange"


def _ts_to_utc(ts: int) -> datetime:
    if ts > 1_000_000_000_000:      # microseconds
        ts = ts / 1_000_000
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class DeltaFeed(DataFeed):
    def __init__(self, base_url: str = None):
        self.base_url = (base_url or os.getenv("DELTA_BASE_URL", DEFAULT_BASE)).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "r3-paper-bot/0.1"})
        self.fut_symbol = "BTCUSD"

    def _get(self, path: str, params: dict = None) -> dict:
        r = self.session.get(f"{self.base_url}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _ticker_close(self, symbol: str):
        data = self._get(f"/v2/tickers/{symbol}")
        res = data.get("result")
        return float(res["close"]) if res and res.get("close") is not None else None

    def futures_price_at(self, ts):
        return self._ticker_close(self.fut_symbol)

    def option_price_at(self, opt_type, strike, expiry, ts):
        return self._ticker_close(self.product_symbol(opt_type, strike, expiry))

    def option_trades(self, opt_type, strike, expiry, ts_from, ts_to):
        symbol = self.product_symbol(opt_type, strike, expiry)
        data = self._get(f"/v2/trades/{symbol}")
        res = data.get("result")
        if isinstance(res, dict):
            trades = res.get("trades") or []
        elif isinstance(res, list):
            trades = res
        else:
            trades = []
        out = []
        for t in trades:
            t_ts = _ts_to_utc(t["timestamp"])
            if ts_from <= t_ts <= ts_to:
                out.append((t_ts, float(t["price"])))
        return sorted(out, key=lambda x: x[0])
