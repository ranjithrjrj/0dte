"""Paper ledger — JSONL trade log + persisted engine state.

Kept as a simple JSON/CSV log (not the TimescaleDB/dashboard), per your choice.
State is persisted so the service can resume cleanly after a VPS reboot.
"""
import json
from pathlib import Path


class Ledger:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trades_file = self.dir / "paper_trades.jsonl"
        self.state_file = self.dir / "paper_state.json"

    def record(self, rec: dict):
        with open(self.trades_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def save_state(self, state: dict):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)

    def load_state(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        return {}

    def load_trades(self) -> list:
        if not self.trades_file.exists():
            return []
        with open(self.trades_file, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
