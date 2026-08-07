"""
Analyze drawdown episodes from the 0DTE backtest and quantify the
"one more loss wipes the account" risk at the deepest troughs.

Usage: .\\.venv\\Scripts\\python analysis\\drawdown_risk.py
"""
import csv
from pathlib import Path

RES = Path("backtest/results")


def load(name):
    with open(RES / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    equity = load("equity.csv")
    trades = load("trades.csv")

    # --- drawdown episodes (peak -> trough -> recovery) ---
    peak = 0.0
    episodes = []
    cur = None
    for e in equity:
        b = float(e["balance"])
        if b > peak:
            if cur is not None and cur["depth"] > 0:
                cur["recovered"] = e["date"]
                episodes.append(cur)
                cur = None
            peak = b
        else:
            dd = peak - b
            if cur is None:
                cur = {"start": None, "peak": peak, "trough": b,
                       "trough_date": e["date"], "depth": dd}
            elif b < cur["trough"]:
                cur["trough"] = b
                cur["trough_date"] = e["date"]
                cur["depth"] = dd
    if cur and cur["depth"] > 0:
        cur["recovered"] = None
        episodes.append(cur)

    episodes.sort(key=lambda x: x["depth"], reverse=True)
    # assign start = first date where balance <= peak-0.0001 ... simpler: find prior date
    bal = [e["balance"] for e in equity]
    dates = [e["date"] for e in equity]
    for ep in episodes:
        idx = dates.index(ep["trough_date"])
        # start = first day of decline into this trough
        s = idx
        while s > 0 and float(bal[s-1]) >= float(bal[s]) or True:
            s -= 1
            if float(bal[s]) > ep["peak"]:
                s += 1
                break
        ep["start"] = dates[max(0, s)]

    print("=" * 100)
    print(f"TOP DRAWDOWN EPISODES  (peak -> trough, USD)")
    print("=" * 100)
    print(f"{'#':<3}{'start':<12}{'trough':<12}{'peak':<10}{'trough_bal':<12}{'dd_usd':<10}{'dd_pct':<10}{'recov':<12}{'days':<6}")
    for i, ep in enumerate(episodes[:8], 1):
        td = dates.index(ep["trough_date"])
        sd = dates.index(ep["start"]) if ep["start"] in dates else td
        days = td - sd
        print(f"{i:<3}{ep['start']:<12}{ep['trough_date']:<12}{ep['peak']:<10.0f}{ep['trough']:<12.2f}"
              f"{ep['depth']:<10.2f}{100*ep['depth']/ep['peak']:<10.1f}{str(ep['recovered']):<12}{days:<6}")

    # --- the key question: at each trough, what would one more loss do? ---
    print("\n" + "=" * 100)
    print("WIPEOUT MARGIN AT EACH TROUGH (one more bad day)")
    print("=" * 100)
    # map trade date -> net
    net = {t["date"]: float(t["net_usd"]) for t in trades}
    lots = {t["date"]: float(t["lots"]) for t in trades}
    entry = {t["date"]: float(t["entry_px"]) for t in trades}

    # typical big-loss size: 90th percentile of losing days
    losses = sorted([n for n in net.values() if n < 0])
    p90_loss = -losses[int(len(losses)*0.9)] if losses else 0
    worst = -min(net.values())

    print(f"\nDistribution of losing days: count={len(losses)}, "
          f"median=${-losses[len(losses)//2]:.0f}, p90=${p90_loss:.0f}, worst=${worst:.0f}")
    print(f"\n{'trough_date':<12}{'balance':<10}{'lots_then':<10}{'entry_px':<9}"
          f"{'loss_p90_pct':<13}{'worst_pct':<10}{'would_p90_wipe':<15}{'would_worst_wipe':<17}")
    for i, ep in enumerate(episodes[:8], 1):
        td = ep["trough_date"]
        b = ep["trough"]
        l = lots.get(td, 0)
        p90_pct = 100*p90_loss/b if b > 0 else float('inf')
        worst_pct = 100*worst/b if b > 0 else float('inf')
        print(f"{td:<12}{b:<10.0f}{l:<10.0f}{entry.get(td, 0):<9.0f}"
              f"{p90_pct:<13.1f}{worst_pct:<10.1f}{'YES' if p90_loss>b else 'no':<15}{'YES' if worst>b else 'no':<17}")

    # --- amplification: lots vs what 0.2% sizing would imply ---
    print("\n" + "=" * 100)
    print("POSITION AMPLIFICATION (lots vs 'normal' 0.2% sizing)")
    print("=" * 100)
    print(f"{'date':<12}{'balance':<10}{'lots':<8}{'normal_lots':<12}{'x_multiple':<11}{'exposure_btc':<12}{'expos_%bal':<11}")
    for t in trades:
        b = float(t["balance"]) - float(t["net_usd"])  # balance BEFORE this trade
        l = float(t["lots"])
        normal = max(1.0, (0.002*b*1000)/max(float(t["entry_px"]), 1))
        # notional exposure in BTC and % of balance (BTC value unknown; use strike*0.001*lots)
        if float(t["balance"]) > 0:
            pass
    # sample the most extreme amplification days
    ampl = []
    for t in trades:
        b_before = float(t["balance"]) - float(t["net_usd"])
        l = float(t["lots"])
        normal = max(1.0, (0.002*b_before*1000)/max(float(t["entry_px"]), 1))
        ampl.append((l/normal, t["date"], b_before, l, normal))
    ampl.sort(reverse=True)
    print("Top 10 amplification days (lots / normal-0.2%-lots):")
    for m, d, b, l, n in ampl[:10]:
        print(f"  {d}: balance_before=${b:.0f}  lots={l:.0f}  normal={n:.1f}  x{m:.1f}")

    # --- streak / equity at bottom: how close to zero ---
    print("\n" + "=" * 100)
    print("HOW CLOSE TO ZERO WAS THE EQUITY AT EACH TROUGH?")
    print("=" * 100)
    for i, ep in enumerate(episodes[:8], 1):
        print(f"  {ep['trough_date']}: balance=${ep['trough']:.2f}  "
              f"({100*ep['trough']/1000:.1f}% of start; {ep['depth']/ep['peak']*100:.0f}% off peak)")


if __name__ == "__main__":
    main()
