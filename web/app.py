"""FastAPI server serving the 0DTE backtest results to the dashboard.

Run:  .\\.venv\\Scripts\\python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
"""
import csv
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "backtest" / "results"
RESULTS_V2 = ROOT / "backtest" / "results_v2"
SWEEP = ROOT / "backtest" / "results_sweep"
REFINE = ROOT / "backtest" / "results_refine"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="0DTE Short-Put Backtest Dashboard")

STRATEGIES = {
    "1": {"dir": RESULTS, "name": "Strategy 1 · 0.2% + carry"},
    "2": {"dir": RESULTS_V2, "name": "Strategy 2 · 1% rolling (no guards)"},
    "2x5":  {"dir": SWEEP / "2x_5pct",  "name": "S3 · stop 2x · cap 5%"},
    "2x8":  {"dir": SWEEP / "2x_8pct",  "name": "S3 · stop 2x · cap 8%"},
    "2x10": {"dir": SWEEP / "2x_10pct", "name": "S3 · stop 2x · cap 10%"},
    "3x5":  {"dir": SWEEP / "3x_5pct",  "name": "S3 · stop 3x · cap 5%"},
    "3x8":  {"dir": SWEEP / "3x_8pct",  "name": "S3 · stop 3x · cap 8%"},
    "3x10": {"dir": SWEEP / "3x_10pct", "name": "S3 · stop 3x · cap 10%"},
    "4x5":  {"dir": SWEEP / "4x_5pct",  "name": "S3 · stop 4x · cap 5%"},
    "4x8":  {"dir": SWEEP / "4x_8pct",  "name": "S3 · stop 4x · cap 8%"},
    "4x10": {"dir": SWEEP / "4x_10pct", "name": "S3 · stop 4x · cap 10%"},
    "r0": {"dir": REFINE / "r0_baseline",       "name": "Refine · baseline 2x/5%"},
    "r1": {"dir": REFINE / "r1_poststop_reset", "name": "Refine · reset after stop"},
    "r2": {"dir": REFINE / "r2_throttle2",      "name": "Refine · throttle after 2 losses"},
    "r3": {"dir": REFINE / "r3_skip",           "name": "Refine · skip day after stop"},
    "r4": {"dir": REFINE / "r4_cap3",           "name": "Refine · cap 3%"},
    "r5": {"dir": REFINE / "r5_floor05",        "name": "Refine · floor 0.5%"},
    "r6": {"dir": REFINE / "r6_reset_throttle", "name": "Refine · reset + throttle"},
    "r7": {"dir": REFINE / "r7_all",            "name": "Refine · all guards"},
    "r8": {"dir": REFINE / "r8_skip_cap4",      "name": "Skip · cap 4%"},
    "r9": {"dir": REFINE / "r9_skip_cap3",      "name": "Skip · cap 3%"},
    "r10": {"dir": REFINE / "r10_skip_throttle", "name": "Skip · throttle"},
    "r11": {"dir": REFINE / "r11_skip_cap4_thr", "name": "Skip · cap4 + throttle"},
}


def _dir(s: str):
    return STRATEGIES.get(str(s), STRATEGIES["1"])["dir"]


def read_csv(name: str, s: str = "1"):
    p = _dir(s) / name
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@app.get("/api/summary")
def api_summary(s: str = "1"):
    p = _dir(s) / "summary.json"
    out = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    out["strategy_name"] = STRATEGIES.get(str(s), STRATEGIES["1"])["name"]
    return out


@app.get("/api/equity")
def api_equity(s: str = "1"):
    return read_csv("equity.csv", s)


@app.get("/api/trades")
def api_trades(s: str = "1"):
    return read_csv("trades.csv", s)


@app.get("/api/drawdown")
def api_drawdown(s: str = "1"):
    eq = read_csv("equity.csv", s)
    peak = 0.0
    out = []
    for e in eq:
        b = float(e["balance"])
        peak = max(peak, b)
        out.append({
            "date": e["date"],
            "balance": round(b, 2),
            "peak": round(peak, 2),
            "drawdown_pct": round(100.0 * (peak - b) / peak, 2) if peak > 0 else 0.0,
            "drawdown_usd": round(peak - b, 2),
        })
    return out


@app.get("/api/compare")
def api_compare():
    """Both strategies' equity + summary for side-by-side comparison."""
    out = {}
    for key, meta in STRATEGIES.items():
        p = meta["dir"] / "summary.json"
        summ = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        eq = []
        with open(meta["dir"] / "equity.csv", encoding="utf-8") as f:
            eq = list(csv.DictReader(f))
        out[key] = {"name": meta["name"], "summary": summ, "equity": eq}
    return out


@app.get("/api/drawdown_episodes")
def api_drawdown_episodes(s: str = "1"):
    """Drawdown episodes + wipeout margin + position amplification."""
    equity = read_csv("equity.csv", s)
    trades = read_csv("trades.csv", s)
    if not equity or not trades:
        return {"episodes": [], "amplification": []}

    # drawdown episodes (peak -> trough)
    peak = 0.0
    peak_date = None
    episodes, cur = [], None
    for e in equity:
        b = float(e["balance"])
        if b > peak:
            if cur is not None and cur["depth"] > 0:
                cur["recovered"] = e["date"]
                episodes.append(cur)
                cur = None
            peak = b
            peak_date = e["date"]
        else:
            dd = peak - b
            if cur is None:
                cur = {"peak": round(peak, 2), "peak_date": peak_date,
                       "trough": round(b, 2), "trough_date": e["date"], "depth": round(dd, 2)}
            elif b < cur["trough"]:
                cur.update({"trough": round(b, 2), "trough_date": e["date"], "depth": round(dd, 2)})
    if cur and cur["depth"] > 0:
        cur["recovered"] = None
        episodes.append(cur)

    dates = [e["date"] for e in equity]
    bals = [float(e["balance"]) for e in equity]
    for ep in episodes:
        idx = dates.index(ep["trough_date"])
        ep["start"] = ep["peak_date"]
        ep["days"] = idx - dates.index(ep["start"]) if ep["start"] in dates else 0
        ep["dd_pct"] = round(100.0 * ep["depth"] / ep["peak"], 1) if ep["peak"] else 0.0

    episodes.sort(key=lambda x: x["depth"], reverse=True)
    episodes = episodes[:10]

    net = {t["date"]: float(t["net_usd"]) for t in trades}
    lots = {t["date"]: float(t["lots"]) for t in trades}
    losses = sorted([n for n in net.values() if n < 0])
    worst = -min(net.values()) if net else 0
    p90 = -losses[int(len(losses) * 0.9)] if losses else worst
    for ep in episodes[:10]:
        b = ep["trough"]
        ep["lots_at_trough"] = lots.get(ep["trough_date"], 0)
        ep["worst_pct_of_bal"] = round(100.0 * worst / b, 1) if b > 0 else 999
        ep["would_worst_wipe"] = worst > b
        ep["would_p90_wipe"] = p90 > b

    # position amplification (lots vs normal 0.2% sizing)
    amplification = []
    for t in trades:
        b_before = float(t["balance"]) - float(t["net_usd"])
        l = float(t["lots"])
        px = max(float(t["entry_px"]), 1)
        normal = max(1.0, (0.002 * b_before * 1000) / px)
        amplification.append({
            "date": t["date"], "lots": l, "normal_lots": round(normal, 1),
            "x": round(l / normal, 1),
        })
    return {"episodes": episodes, "amplification": amplification, "worst_day_usd": round(worst, 2)}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
