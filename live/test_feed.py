"""Quick connectivity test against the live Delta India API.

Usage:  .\\.venv\\Scripts\\python live\\test_feed.py
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from delta_feed import DeltaFeed


def main():
    feed = DeltaFeed()
    print("base_url:", feed.base_url)

    # 1) BTCUSD futures
    fut = feed.futures_price_at(None)
    print(f"\nBTCUSD futures LTP: {fut}")

    # 2) ATM put for the NEXT daily expiry
    expiry = date.today() + timedelta(days=1)
    strike = int(round(fut / 200)) * 200
    symbol = feed.product_symbol("P", strike, expiry)
    print(f"ATM put symbol: {symbol}  (expiry {expiry})")

    # 3) option LTP
    px = feed.option_price_at("P", strike, expiry, None)
    print(f"option LTP: {px}")

    # 4) recent trades for that option
    from datetime import datetime, timezone, timedelta as td
    now = datetime.now(timezone.utc)
    trades = feed.option_trades("P", strike, expiry, now - td(days=1), now)
    print(f"recent option trades in last 24h: {len(trades)}")
    for t in trades[-5:]:
        print(f"   {t[0]}  price={t[1]}")

    # 5) a ticker's quotes/greeks (option market data available)
    data = feed._get(f"/v2/tickers/{symbol}")
    res = data.get("result") or {}
    print("ticker quotes:", res.get("quotes"))
    print("ticker greeks:", res.get("greeks"))


if __name__ == "__main__":
    main()
