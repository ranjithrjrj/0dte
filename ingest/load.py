"""
Load Delta Exchange India BTC options + futures tick trades into TimescaleDB.

Usage (from repo root, with .venv active):
    python ingest/load.py init            # create tables, hypertables, indexes
    python ingest/load.py load            # load ALL monthly CSVs
    python ingest/load.py load --limit 100000   # quick test on first N rows/file
    python ingest/load.py load --only BTC_2025-12.csv   # single file
    python ingest/load.py compress        # enable TimescaleDB compression
    python ingest/load.py verify          # row counts + basic sanity checks
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:admin@localhost:5432/delta")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# Option symbol: [C|P]-BTC-<strike>-<DDMMYY>  e.g. P-BTC-72800-010424
OPT_RE = re.compile(r"^(?P<typ>[CP])-BTC-(?P<strike>\d+)-(?P<exp>\d{6})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS options_trades (
    ts         TIMESTAMPTZ    NOT NULL,
    opt_type   CHAR(1)        NOT NULL,   -- 'C' | 'P'
    strike     NUMERIC        NOT NULL,
    expiry     DATE           NOT NULL,   -- parsed from DDMMYY
    price      DOUBLE PRECISION NOT NULL, -- USD premium
    size       DOUBLE PRECISION NOT NULL, -- contracts
    buyer_role TEXT           NOT NULL    -- maker|taker (buyer's role)
);

CREATE TABLE IF NOT EXISTS futures_trades (
    ts         TIMESTAMPTZ    NOT NULL,
    price      DOUBLE PRECISION NOT NULL, -- perpetual price USD
    size       DOUBLE PRECISION NOT NULL, -- contracts
    buyer_role TEXT           NOT NULL
);

CREATE TABLE IF NOT EXISTS funding (
    ts         TIMESTAMPTZ    NOT NULL,
    rate       DOUBLE PRECISION NOT NULL
);

SELECT create_hypertable('options_trades', 'ts',
       chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE);
SELECT create_hypertable('futures_trades', 'ts',
       chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE);
SELECT create_hypertable('funding', 'ts',
       chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_options_chain ON options_trades (expiry, strike, ts);
CREATE INDEX IF NOT EXISTS idx_options_ts    ON options_trades (ts);
CREATE INDEX IF NOT EXISTS idx_futures_ts    ON futures_trades (ts);
"""


def parse_ts_fast(s: str) -> datetime:
    """'2024-04-01 00:04:34.371953' (IST) -> tz-aware UTC datetime."""
    y = int(s[0:4]); mo = int(s[5:7]); d = int(s[8:10])
    h = int(s[11:13]); mi = int(s[14:16]); se = int(s[17:19])
    us = int(s[20:]) if len(s) > 19 else 0
    dt = datetime(y, mo, d, h, mi, se, us, tzinfo=IST)
    return dt.astimezone(UTC)


def parse_opt_symbol(sym: str):
    """Return (opt_type, strike, expiry_date_str) or None if malformed."""
    m = OPT_RE.match(sym)
    if not m:
        return None
    typ = m.group("typ")
    strike = int(m.group("strike"))
    e = m.group("exp")  # DDMMYY
    day, month = int(e[0:2]), int(e[2:4])
    year = 2000 + int(e[4:6])
    return typ, strike, f"{year:04d}-{month:02d}-{day:02d}"


def iter_options_rows(path: Path, limit=None):
    """Yield tuples (ts, opt_type, strike, expiry, price, size, buyer_role)."""
    n = 0
    with open(path, "r", encoding="utf-8") as f:
        header = f.readline()
        if not header.startswith("product_symbol"):
            print(f"  ! unexpected header in {path.name}: {header!r}")
        for line in f:
            line = line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            sym, price, size, ts_str, role = line.split(",")
            parsed = parse_opt_symbol(sym)
            if parsed is None:
                print(f"  ! unparsable symbol: {sym} in {path.name}")
                continue
            typ, strike, expiry = parsed
            yield (parse_ts_fast(ts_str), typ, strike, expiry,
                   float(price), float(size), role)
            n += 1
            if limit and n >= limit:
                return


def iter_futures_rows(path: Path, limit=None):
    """Yield tuples (ts, price, size, buyer_role)."""
    n = 0
    with open(path, "r", encoding="utf-8") as f:
        header = f.readline()
        if not header.startswith("product_symbol"):
            print(f"  ! unexpected header in {path.name}: {header!r}")
        for line in f:
            line = line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            sym, price, size, ts_str, role = line.split(",")
            yield (parse_ts_fast(ts_str), float(price), float(size), role)
            n += 1
            if limit and n >= limit:
                return


def copy_rows(conn, table, cols, rows, label):
    sql = f"COPY {table} ({', '.join(cols)}) FROM STDIN"
    start = time.perf_counter()
    with conn.cursor() as cur:
        with cur.copy(sql) as copy:
            for row in rows:
                copy.write_row(row)
    elapsed = time.perf_counter() - start
    print(f"  loaded {label} in {elapsed:.1f}s")


def init():
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
        # confirm hypertables
        rows = conn.execute(
            "SELECT hypertable_name, num_dimensions FROM timescaledb_information.hypertables "
            "ORDER BY hypertable_name;"
        ).fetchall()
        print("Hypertables ready:")
        for r in rows:
            print(f"  - {r[0]} (dims={r[1]})")


def load_file(path: Path, limit=None):
    is_options = path.name.startswith("BTC_")
    table = "options_trades" if is_options else "futures_trades"
    cols = ("ts", "opt_type", "strike", "expiry", "price", "size", "buyer_role") if is_options \
        else ("ts", "price", "size", "buyer_role")
    gen = iter_options_rows(path, limit) if is_options else iter_futures_rows(path, limit)
    with psycopg.connect(DATABASE_URL) as conn:
        copy_rows(conn, table, cols, gen, path.name)


def load(only=None, limit=None):
    files = sorted(DATA_DIR.glob("BTC_*.csv")) + sorted(DATA_DIR.glob("BTCUSD_*.csv"))
    if only:
        files = [f for f in files if f.name in only]
    if not files:
        print("No matching CSV files found in", DATA_DIR)
        return
    for path in files:
        t0 = time.perf_counter()
        print(f"[{time.strftime('%H:%M:%S')}] {path.name} ...")
        load_file(path, limit=limit)
        print(f"    done in {time.perf_counter() - t0:.1f}s")


def compress():
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("""
            ALTER TABLE options_trades SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'opt_type, strike, expiry',
                timescaledb.compress_orderby = 'ts'
            );
            ALTER TABLE futures_trades SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'buyer_role',
                timescaledb.compress_orderby = 'ts'
            );
        """)
        conn.commit()
        for tbl in ("options_trades", "futures_trades"):
            conn.execute(f"SELECT compress_chunk(c, if_not_compressed => true) "
                         f"FROM show_chunks('{tbl}') c;")
        conn.commit()
        print("Compression enabled + all chunks compressed.")


def verify():
    with psycopg.connect(DATABASE_URL) as conn:
        for tbl in ("options_trades", "futures_trades"):
            total = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            mn, mx = conn.execute(f"SELECT min(ts), max(ts) FROM {tbl}").fetchone()
            print(f"{tbl}: {total:,} rows  |  {mn} -> {mx}")
        print("\nPer-month options rows:")
        for r in conn.execute(
            "SELECT to_char(date_trunc('month', ts), 'YYYY-MM'), count(*) "
            "FROM options_trades GROUP BY 1 ORDER BY 1").fetchall():
            print(f"  {r[0]}: {r[1]:,}")
        print("\nPer-month futures rows:")
        for r in conn.execute(
            "SELECT to_char(date_trunc('month', ts), 'YYYY-MM'), count(*) "
            "FROM futures_trades GROUP BY 1 ORDER BY 1").fetchall():
            print(f"  {r[0]}: {r[1]:,}")


def main():
    ap = argparse.ArgumentParser(description="Delta Exchange tick data loader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("load")
    p.add_argument("--only", action="append", help="filename(s) to load, repeatable")
    p.add_argument("--limit", type=int, default=None, help="max rows per file (test)")
    sub.add_parser("compress")
    sub.add_parser("verify")
    args = ap.parse_args()

    if args.cmd == "init":
        init()
    elif args.cmd == "load":
        load(only=args.only, limit=args.limit)
    elif args.cmd == "compress":
        compress()
    elif args.cmd == "verify":
        verify()


if __name__ == "__main__":
    main()
