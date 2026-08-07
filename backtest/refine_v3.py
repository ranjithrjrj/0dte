"""
Run drawdown-reduction refinements of the 2x/5% version.

Each writes to backtest/results_refine/<id>/. Shells out to 0dte_v3.py.

Usage:  .\\.venv\\Scripts\\python backtest\\refine_v3.py
"""
import subprocess
from pathlib import Path

# (id, extra args on top of 2x / 5%)
VARIANTS = [
    ("r0_baseline",        []),
    ("r1_poststop_reset",  ["--post_stop_reset"]),
    ("r2_throttle2",       ["--loss_throttle", "2"]),
    ("r3_skip",            ["--post_stop_skip"]),
    ("r4_cap3",            ["--cap", "0.03"]),
    ("r5_floor05",         ["--floor", "0.005"]),
    ("r6_reset_throttle",  ["--post_stop_reset", "--loss_throttle", "2"]),
    ("r7_all",             ["--post_stop_reset", "--loss_throttle", "2", "--post_stop_skip"]),
]

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def main():
    for vid, extra in VARIANTS:
        print(f"\n##### {vid} #####", flush=True)
        r = subprocess.run(
            [str(PY), "backtest/0dte_v3.py", "--stop", "2.0", "--cap", "0.05",
             "--results", f"backtest/results_refine/{vid}"] + extra,
            cwd=ROOT, capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines()[-6:]:
            print("   " + line, flush=True)


if __name__ == "__main__":
    main()
