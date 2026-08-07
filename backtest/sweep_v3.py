"""
Run the strategy-3 parameter sweep: stop x {2,3,4}  x  cap {5,8,10}%.

Each combo is written to backtest/results_sweep/<stop>x_<cap>pct/ so the
dashboard can list them as separate versions. Shells out to 0dte_v3.py
(via --stop/--cap) to avoid the digit-prefixed module-name import issue.

Usage:  .\\.venv\\Scripts\\python backtest\\sweep_v3.py
"""
import subprocess
from pathlib import Path

COMBOS = [
    (2.0, 0.05), (2.0, 0.08), (2.0, 0.10),
    (3.0, 0.05), (3.0, 0.08), (3.0, 0.10),
    (4.0, 0.05), (4.0, 0.08), (4.0, 0.10),
]

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def main():
    for stop, cap in COMBOS:
        print(f"\n########## STOP {stop:g}x  ·  CAP {cap*100:.0f}% ##########", flush=True)
        r = subprocess.run(
            [str(PY), "backtest/0dte_v3.py", "--stop", str(stop), "--cap", str(cap)],
            cwd=ROOT, capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines()[-8:]:
            print("   " + line, flush=True)


if __name__ == "__main__":
    main()
