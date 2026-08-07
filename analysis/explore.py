"""
Exploratory analysis of loaded Delta Exchange data.
Run AFTER `python ingest/load.py load`:

    python analysis/explore.py            # full summary
    python analysis/explore.py --month 2025-12   # focus one month
"""
import argparse
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:admin@localhost:5432/delta")


def q(conn, sql, label, params=None):
    rows = conn.execute(sql, params or ()).fetchall()
    print(f"\n== {label} ==")
    if not rows:
        print("  (no rows)")
        return
    # column names
    cols = [d.name for d in conn.execute(sql, params or ()).description]
    widths = {c: max(len(c), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)}
    header = "  " + "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        print("  " + "  ".join(str(r[i]).ljust(widths[c]) for i, c in enumerate(cols)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default=None, help="YYYY-MM filter")
    args = ap.parse_args()
    m = args.month
    wh = "WHERE ts >= %s AND ts < %s + INTERVAL '1 month'" if m else "WHERE true"
    params = [m] if m else []

    with psycopg.connect(DATABASE_URL) as conn:
        print("=" * 60)
        print("DELTA EXCHANGE DATA — EXPLORATION")
        print(f"filter month: {m or 'ALL'}")
        print("=" * 60)

        q(conn, "SELECT 'options' AS tbl, count(*) AS rows, min(ts) AS first, max(ts) AS last FROM options_trades "
                "UNION ALL SELECT 'futures', count(*), min(ts), max(ts) FROM futures_trades", "Totals")

        q(conn, f"""SELECT to_char(date_trunc('month', ts),'YYYY-MM') AS month,
                   count(*) AS opt_rows,
                   count(DISTINCT (opt_type, strike, expiry)) AS contracts
                   FROM options_trades {wh}
                   GROUP BY 1 ORDER BY 1""", "Options rows & distinct contracts by month", params)

        q(conn, f"""SELECT to_char(date_trunc('month', ts),'YYYY-MM') AS month, count(*) AS fut_rows
                   FROM futures_trades {wh} GROUP BY 1 ORDER BY 1""", "Futures rows by month", params)

        q(conn, f"""SELECT buyer_role, count(*) AS trades,
                   round(100.0*count(*)/sum(count(*)) OVER (), 2) AS pct
                   FROM options_trades {wh} GROUP BY 1 ORDER BY 2 DESC""", "Options aggressor flow (buyer_role)", params)

        q(conn, f"""SELECT buyer_role, count(*) AS trades,
                   round(100.0*count(*)/sum(count(*)) OVER (), 2) AS pct
                   FROM futures_trades {wh} GROUP BY 1 ORDER BY 2 DESC""", "Futures aggressor flow (buyer_role)", params)

        q(conn, f"""SELECT extract(dow FROM ts) AS dow, count(*) AS trades
                   FROM futures_trades {wh} GROUP BY 1 ORDER BY 1""", "Futures trades by day-of-week (0=Sun)", params)

        q(conn, f"""SELECT opt_type, min(price) AS min_px, max(price) AS max_px,
                   round(avg(price)::numeric,2) AS avg_px, count(*) AS n
                   FROM options_trades {wh} GROUP BY 1""", "Options premium range by type", params)

        q(conn, f"""SELECT min(ts), max(ts), count(DISTINCT expiry) AS expiries
                   FROM options_trades {wh}""", "Options expiry span & count", params)

        q(conn, f"""SELECT expiry, count(*) AS trades,
                   round(avg(strike)) AS avg_strike, count(DISTINCT strike) AS strikes
                   FROM options_trades {wh}
                   GROUP BY 1 ORDER BY 1 DESC LIMIT 10""", "Top-10 expiries by trades", params)

        q(conn, f"""SELECT min(price) AS min_px, max(price) AS max_px, round(avg(price)::numeric,2) AS avg_px,
                   count(*) AS n FROM futures_trades {wh}""", "Futures price range", params)


if __name__ == "__main__":
    main()
