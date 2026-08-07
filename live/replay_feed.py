"""ReplayFeed — serves historical data from TimescaleDB.

Used to validate the paper engine against the r3 backtest before going live.
"""
import os

import psycopg
from dotenv import load_dotenv

from datafeed import DataFeed

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:admin@localhost:5432/delta")


class ReplayFeed(DataFeed):
    def __init__(self, dsn: str = None):
        self.dsn = dsn or DATABASE_URL

    def _conn(self):
        return psycopg.connect(self.dsn)

    def futures_price_at(self, ts):
        with self._conn() as c:
            r = c.execute(
                "SELECT price FROM futures_trades WHERE ts <= %s ORDER BY ts DESC LIMIT 1",
                (ts,)).fetchone()
            return float(r[0]) if r else None

    def option_price_at(self, opt_type, strike, expiry, ts):
        with self._conn() as c:
            r = c.execute(
                "SELECT price FROM options_trades "
                "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts<=%s "
                "ORDER BY ts DESC LIMIT 1", (opt_type, strike, expiry, ts)).fetchone()
            return float(r[0]) if r else None

    def option_trades(self, opt_type, strike, expiry, ts_from, ts_to):
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, price FROM options_trades "
                "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts>=%s AND ts<=%s "
                "ORDER BY ts", (opt_type, strike, expiry, ts_from, ts_to)).fetchall()
            return [(r[0], float(r[1])) for r in rows]
