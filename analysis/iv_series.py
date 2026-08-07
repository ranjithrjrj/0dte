"""Build a daily ATM 0DTE implied-vol series + IVP (IV percentile) from option ticks.

Method:
  - For each day, sample the ATM PUT expiring NEXT day around 17:00 IST
    (the r3 entry time). ATM strike = round(fut17/200)*200 from the futures price.
  - Back out Black-Scholes implied vol from the median put premium.
  - IVP = trailing-365-day percentile rank of today's IV (1..100).
  - Also test: does gating a simple short-premium trade on IVP improve results?

Run:  .\\.venv\\Scripts\\python analysis\\iv_series.py [--out data/iv_series.csv]
"""
import argparse
import math
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:admin@localhost:5432/delta")
IST = timezone(timedelta(hours=5, minutes=30))
CONTRACT = 0.001
TAKER = 0.0005
GST = 0.18
W_START = time(16, 30)   # IST
W_END = time(17, 0)      # IST


def month_chunks(d0, d1):
    out = []
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        start = datetime(y, m, 1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((start, datetime(ny, nm, 1)))
        y, m = ny, nm
    return out


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_put(S, K, T, sig):
    if sig <= 0 or S <= 0 or K <= 0:
        return float("nan")
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return K * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_iv(S, K, T, px, lo=0.01, hi=6.0):
    for _ in range(120):
        mid = (lo + hi) / 2
        v = bs_put(S, K, T, mid)
        if v > px:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return (lo + hi) / 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/iv_series.csv")
    args = ap.parse_args()

    conn = psycopg.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT min(ts)::date, max(ts)::date FROM options_trades")
    d0, d1 = cur.fetchone()
    cur.execute("SELECT min(ts)::date, max(ts)::date FROM futures_trades")
    f0, f1 = cur.fetchone()

    rows = []
    for start, end in month_chunks(d0, d1):
        fut = pd.read_sql("SELECT ts, price FROM futures_trades WHERE ts>=%s AND ts<%s ORDER BY ts",
                          conn, params=(start, end))
        if fut.empty:
            continue
        fut["ts"] = pd.to_datetime(fut["ts"], utc=True)
        opt = pd.read_sql("SELECT ts, strike, expiry, price FROM options_trades "
                          "WHERE ts>=%s AND ts<%s AND opt_type='P' ORDER BY ts",
                          conn, params=(start, end))
        if opt.empty:
            continue
        opt["ts"] = pd.to_datetime(opt["ts"], utc=True)
        opt["strike"] = opt["strike"].astype(float)
        # per day
        for d in sorted(set(fut["ts"].dt.date) & set(opt["ts"].dt.date)):
            lo = datetime.combine(d, W_START, tzinfo=IST).astimezone(timezone.utc)
            hi = datetime.combine(d, W_END, tzinfo=IST).astimezone(timezone.utc)
            f = fut[(fut["ts"] >= lo) & (fut["ts"] < hi)]
            if f.empty:
                continue
            fut17 = f.iloc[-1]["price"]
            K = int(round(fut17 / 200)) * 200
            exp = d + timedelta(days=1)
            o = opt[(opt["expiry"] == exp) & (opt["strike"] == K) &
                    (opt["ts"] >= lo) & (opt["ts"] < hi)]
            if o.empty:
                continue
            prem = float(o["price"].median())
            T = 24.5 / 8760.0  # 17:00 IST today -> 17:30 IST tomorrow, years
            iv = bs_iv(fut17, K, T, prem)
            rows.append({"date": d, "fut": fut17, "strike": K, "premium": round(prem, 2),
                         "iv": iv})
        print(f"  ... {start:%Y-%m}", flush=True)
    conn.close()

    df = pd.DataFrame(rows).set_index("date")
    df = df.sort_index()
    df["ivp"] = df["iv"].rolling(365, min_periods=120).apply(
        lambda x: 100 * (x <= x.iloc[-1]).mean(), raw=False).shift(1)  # trailing, ex today
    df = df.dropna(subset=["ivp"])
    df.to_csv(args.out)
    print(f"\nsaved {len(df)} daily IV points -> {args.out}")
    print(df[["fut", "strike", "premium", "iv", "ivp"]].tail(8).to_string())

    print("\n=== IV SUMMARY ===")
    print(f"  IV  min/med/max: {df['iv'].min():.2f} / {df['iv'].median():.2f} / {df['iv'].max():.2f}")
    for q in (25, 50, 75, 90):
        print(f"  IVP>={q}: {int((df['ivp'] >= q).sum())} days  "
              f"({100*(df['ivp'] >= q).mean():.0f}%)")

    print("\n=== HIGH-IVP PERIODS (IVP>=85, contiguous) ===")
    hi_ivp = df["ivp"] >= 85
    runs, cur_run = [], []
    for d, v in hi_ivp.items():
        if v:
            cur_run.append(d)
        elif cur_run:
            if len(cur_run) >= 2:
                runs.append((cur_run[0], cur_run[-1], len(cur_run)))
            cur_run = []
    if cur_run and len(cur_run) >= 2:
        runs.append((cur_run[0], cur_run[-1], len(cur_run)))
    for r in runs[-15:]:
        print(f"  {r[0]} -> {r[1]}  ({r[2]}d)")

    gated_test(df, args)
    print("\nDONE")


def gated_test(df, args):
    """Simple short ATM-put test, gated on IVP. Entry 17:00 IST day d, exit 17:00 day d+1."""
    # reuse the iv-series prem as entry premium; need exit premium = next day's 0DTE ATM put
    # (at 17:00 on d+1, the option expiring d+2 is the one r3 would trade; but this simple
    # test uses the same chain -> use next day's iv premium as an approximation).
    bal = 1000.0
    print("\n=== GATED SHORT-PREMIUM TEST (simple: short ATM put, exit next 17:00) ===")
    print(f"{'gate':>5} {'trades':>7} {'final':>9} {'ret%':>7} {'win%':>6} {'PF':>5}")
    for gate in (0, 50, 70, 85):
        b = 1000.0
        nets = []
        for i in range(1, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]
            if prev["ivp"] < gate:
                continue
            entry = prev["premium"] - 0.5
            exit = row["premium"] + 0.5
            lots = max(1, round(0.01 * b))
            gross = (entry - exit) * lots * CONTRACT
            fee = lots * CONTRACT * (entry + exit) * TAKER * (1 + GST)
            net = gross - fee
            b += net
            nets.append(net)
        n = len(nets)
        wins = sum(1 for x in nets if x > 0)
        gp = sum(x for x in nets if x > 0)
        gl = -sum(x for x in nets if x < 0)
        pf = round(gp / gl, 2) if gl > 0 else None
        print(f"{gate:>5} {n:>7} {b:>9,.0f} {100*(b-1000)/1000:>+7.1f} "
              f"{100*wins/max(n,1):>6.1f} {str(pf):>5}")


if __name__ == "__main__":
    main()
