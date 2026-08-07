"""
Refine the r3 winner further: skip-after-stop combined with a tighter cap
and/or a mild consecutive-loss throttle.

Writes to backtest/results_refine/r{8..11}_*. Shells out to 0dte_v3.py.

Usage:  .\\.venv\\Scripts\\python backtest\\refine_r3.py
"""
import subprocess
from pathlib import Path

# (id, extra args on top of stop 2x / cap 5% / skip)
VARIANTS = [
    ("r8_skip_cap4",        ["--post_stop_skip", "--cap", "0.04"]),
    ("r9_skip_cap3",        ["--post_stop_skip", "--cap", "0.03"]),
    ("r10_skip_throttle",   ["--post_stop_skip", "--loss_throttle", "2"]),
    ("r11_skip_cap4_thr",   ["--post_stop_skip", "--cap", "0.04", "--loss_throttle", "2"]),
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
