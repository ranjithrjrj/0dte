r"""Paper-trading CLI.

  python run.py replay [--start 2024-04-01 --end 2026-07-25 --results DIR]
        Replays the r3 engine over historical data (validates vs the backtest).

  python run.py live [--results DIR]
        Live paper trading loop. Requires the Delta connector (pending API facts).

Run:  .\.venv\Scripts\python live\run.py replay ...
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paper_engine import PaperEngine, PaperConfig, at_ist
from ledger import Ledger

# Ledger/state directory (set PAPER_RESULTS_DIR to a mounted volume on Railway/VPS)
RESULTS_DIR = os.getenv("PAPER_RESULTS_DIR", "live/paper_data")


def summarize(records):
    trades = [r for r in records if not r.get("skip")]
    n = len(trades)
    if not n:
        return {"n_trades": 0}
    net = sum(t["net_usd"] for t in trades)
    wins = [t for t in trades if t["net_usd"] > 0]
    losses = [t for t in trades if t["net_usd"] < 0]
    gp = sum(t["net_usd"] for t in wins)
    gl = -sum(t["net_usd"] for t in losses)
    return {
        "n_trades": n,
        "net_usd": round(net, 2),
        "final_balance": trades[-1]["balance"],
        "total_return_pct": round(100 * net / trades[0]["balance"], 2),
        "win_rate_pct": round(100 * len(wins) / n, 2),
        "profit_factor": round(gp / gl, 2) if gl > 0 else float("inf"),
        "n_stops": sum(1 for t in trades if t["exit_type"] == "stop"),
        "exits": dict(Counter(t["exit_type"] for t in trades)),
    }


def cmd_replay(args):
    from replay_feed import ReplayFeed
    feed = ReplayFeed()
    cfg = PaperConfig()
    engine = PaperEngine(cfg, feed)
    ledger = Ledger(args.results or RESULTS_DIR)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    print(f"Replaying r3  {start} -> {end}  (feed: TimescaleDB replay)")
    d = start
    n_trade, n_skip = 0, 0
    while d <= end:
        rec = engine.step(d)
        ledger.record(rec)
        if rec.get("skip"):
            n_skip += 1
        else:
            n_trade += 1
        d += timedelta(days=1)
    ledger.save_state(engine.state())
    summary = summarize(ledger.load_trades())
    print(json.dumps(summary, indent=2))
    print(f"traded days={n_trade} skipped={n_skip}  final balance=${engine.balance:,.2f}")


def cmd_live(args):
    from delta_feed import DeltaFeed
    feed = DeltaFeed()
    cfg = PaperConfig()
    engine = PaperEngine(cfg, feed)
    ledger = Ledger(args.results or RESULTS_DIR)
    engine.load_state(ledger.load_state())
    poll_secs = int(os.getenv("POLL_SECONDS", "15"))
    print(f"Live paper scheduler starting  ({feed.base_url})")
    print(f"  balance=${engine.balance:.2f}  open_position={engine.position is not None}  poll={poll_secs}s")
    # monitoring dashboard (http://host:$PORT)
    try:
        from monitor import start_monitor
        httpd = start_monitor(args.results or RESULTS_DIR, engine=engine)
        print(f"  monitor: dashboard listening on :{httpd.server_port}", flush=True)
    except Exception as e:
        print(f"  monitor: disabled ({e})", flush=True)
    last_action_date = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            today = now.date()
            if engine.position is not None:
                rec = engine.poll(now)
                if rec:
                    ledger.record(rec); ledger.save_state(engine.state())
                    print(f"[{now:%Y-%m-%d %H:%M:%S}] EXIT {rec['exit_type']}  "
                          f"net=${rec['net_usd']:.2f}  bal=${rec['balance']:.2f}", flush=True)
                elif now >= engine.position["close_ts"]:
                    rec = engine.close_position()
                    ledger.record(rec); ledger.save_state(engine.state())
                    print(f"[{now:%Y-%m-%d %H:%M:%S}] CLOSE {rec['exit_type']}  "
                          f"net=${rec['net_usd']:.2f}  bal=${rec['balance']:.2f}", flush=True)
            else:
                entry_ts = at_ist(today, cfg.entry_time_ist)
                if now >= entry_ts and today != last_action_date:
                    rec = engine.open_position(today)
                    ledger.record(rec); ledger.save_state(engine.state())
                    last_action_date = today
                    if rec.get("skip"):
                        print(f"[{now:%Y-%m-%d %H:%M:%S}] SKIP  {rec.get('reason')}", flush=True)
                    else:
                        print(f"[{now:%Y-%m-%d %H:%M:%S}] OPEN {rec['symbol']}  "
                              f"lots={rec['lots']} @{rec['entry_px']}  bal=${engine.balance:.2f}", flush=True)
        except Exception as e:
            print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}] scheduler error: {e}", flush=True)
        time.sleep(poll_secs)


def main():
    ap = argparse.ArgumentParser(description="r3 paper trading")
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("replay")
    p.add_argument("--start", default="2024-04-01")
    p.add_argument("--end", default="2026-07-25")
    p.add_argument("--results", default=None)
    q = sub.add_parser("live")
    q.add_argument("--results", default=None)
    args = ap.parse_args()

    if args.mode == "replay":
        cmd_replay(args)
    else:
        cmd_live(args)


if __name__ == "__main__":
    main()
