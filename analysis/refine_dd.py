"""Diagnose the drawdown drivers of the 2x/5% strategy version.

Usage: .\\.venv\\Scripts\\python analysis\\refine_dd.py
"""
import csv
from collections import Counter
from pathlib import Path

RES = Path("backtest/results_sweep/2x_5pct")


def load(name):
    with open(RES / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    tr = load("trades.csv")
    eq = load("equity.csv")

    # ---- consecutive losing streaks ----
    streak = 0
    max_streak = 0
    streaks = []
    for t in tr:
        if float(t["net_usd"]) < 0:
            streak += 1
        else:
            if streak >= 2:
                streaks.append((t["date"], streak))
            max_streak = max(max_streak, streak)
            streak = 0
    if streak >= 2:
        streaks.append((tr[-1]["date"], streak))
    max_streak = max(max_streak, streak)
    print("max consecutive losing days:", max_streak)
    print("streaks >= 2:", len(streaks), "last 5:", streaks[-5:])

    # ---- losing days by exit type ----
    c = Counter(t["exit_type"] for t in tr if float(t["net_usd"]) < 0)
    print("losing days by exit:", dict(c))
    agg = {}
    for t in tr:
        if float(t["net_usd"]) < 0:
            agg.setdefault(t["exit_type"], []).append(float(t["net_usd"]))
    for k, v in agg.items():
        print(f"  {k}: n={len(v)}  avg_loss=${sum(v)/len(v):.2f}  worst=${min(v):.2f}")

    # ---- biggest position days ----
    big = sorted(tr, key=lambda t: float(t["lots"]), reverse=True)[:8]
    print("\nbiggest position days (at cap size):")
    for t in big:
        print(f"  {t['date']}  lots={float(t['lots']):.0f}  exit={t['exit_type']}  net=${float(t['net_usd']):.0f}")

    # ---- drawdown episodes ----
    peak = 0.0
    eps, cur = [], None
    for e in eq:
        b = float(e["balance"])
        if b > peak:
            if cur and cur["depth"] > 0:
                cur["rec"] = e["date"]
                eps.append(cur)
                cur = None
            peak = b
        else:
            dd = peak - b
            if cur is None:
                cur = {"peak": peak, "trough": b, "td": e["date"], "depth": dd}
            elif b < cur["trough"]:
                cur.update({"trough": b, "td": e["date"], "depth": dd})
    if cur and cur["depth"] > 0:
        eps.append(cur)
    eps.sort(key=lambda x: x["depth"], reverse=True)
    print("\ntop drawdown episodes:")
    for ep in eps[:5]:
        print(f"  trough {ep['td']}: peak=${ep['peak']:.0f} -> ${ep['trough']:.0f}  ({100*ep['depth']/ep['peak']:.1f}%)  recovered {ep.get('rec')}")
        # trades in the 5 days before trough
        dates = [t["date"] for t in tr]
        idx = dates.index(ep["td"])
        print("    preceding days:")
        for t in tr[max(0, idx - 4):idx + 1]:
            print(f"      {t['date']} exit={t['exit_type']:<6} lots={float(t['lots']):>6.0f} net=${float(t['net_usd']):>8.2f} bal=${float(t['balance']):>9.2f}")


if __name__ == "__main__":
    main()
