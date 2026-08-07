"""IVP-gated short-premium test, from the saved daily IV series.

Simple daily test: short the ATM put (expiry next day) at 17:00 IST, collect
premium, exit next day 17:00 IST at that day's ATM premium (approximation).
Gates entry on IVP (trailing 365-day IV percentile). Taker fees + GST both sides.

Run:  .\\.venv\\Scripts\\python analysis\\ivp_test.py
"""
import pandas as pd

CONTRACT = 0.001
TAKER = 0.0005
GST = 0.18

df = pd.read_csv("data/iv_series.csv")
df = df.set_index("date").sort_index()
df["premium"] = pd.to_numeric(df["premium"], errors="coerce")
df["ivp"] = pd.to_numeric(df["ivp"], errors="coerce")
df["iv"] = pd.to_numeric(df["iv"], errors="coerce")

print(f"daily IV points: {len(df)}  ({df.index.min()} .. {df.index.max()})")
print(f"IV distribution: min={df['iv'].min():.2f}  p25={df['iv'].quantile(.25):.2f}  "
      f"med={df['iv'].median():.2f}  p75={df['iv'].quantile(.75):.2f}  max={df['iv'].max():.2f}")
print(f"premium distribution: p25={df['premium'].quantile(.25):.1f}  "
      f"med={df['premium'].median():.1f}  p75={df['premium'].quantile(.75):.1f} pts\n")

print("=== GATED SHORT-ATM-PUT TEST (entry gated on IVP) ===")
print(f"{'gate':>5} {'trades':>7} {'final':>9} {'ret%':>7} {'win%':>6} {'PF':>5} "
      f"{'avg prem':>9} {'avg net':>8}")
for gate in (0, 40, 50, 60, 70, 80, 85, 90):
    b = 1000.0
    nets, prems = [], []
    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        row = df.iloc[i]
        if pd.isna(prev["ivp"]) or prev["ivp"] < gate:
            continue
        entry = prev["premium"] - 0.5
        exit = row["premium"] + 0.5
        lots = max(1, round(0.01 * b))
        gross = (entry - exit) * lots * CONTRACT
        fee = lots * CONTRACT * (entry + exit) * TAKER * (1 + GST)
        net = gross - fee
        b += net
        nets.append(net)
        prems.append(prev["premium"])
    n = len(nets)
    wins = sum(1 for x in nets if x > 0)
    gp = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x < 0)
    pf = round(gp / gl, 2) if gl > 0 else None
    avg_prem = sum(prems) / max(n, 1)
    avg_net = sum(nets) / max(n, 1)
    print(f"{gate:>5} {n:>7} {b:>9,.0f} {100*(b-1000)/1000:>+7.1f} "
          f"{100*wins/max(n,1):>6.1f} {str(pf):>5} {avg_prem:>9.1f} {avg_net:>+8.2f}")
