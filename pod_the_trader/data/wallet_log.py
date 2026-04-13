"""Wallet snapshot log: periodic on-chain wallet balance snapshots.

Records the actual SOL + target token balance at each cycle so we can
reconcile the ledger-implied position against on-chain reality, and detect
external transfers.
"""

import csv
import logging
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

WALLET_COLUMNS = [
    "timestamp",
    "wallet",
    "sol_balance",
    "sol_value_usd",
    "token_mint",
    "token_balance",
    "token_decimals",
    "token_price_usd",
    "token_value_usd",
    "total_value_usd",
]


@dataclass
class WalletSnapshot:
    timestamp: str = ""
    wallet: str = ""
    sol_balance: float = 0.0
    sol_value_usd: float = 0.0
    token_mint: str = ""
    token_balance: float = 0.0
    token_decimals: int = 0
    token_price_usd: float = 0.0
    token_value_usd: float = 0.0
    total_value_usd: float = 0.0

    def to_row(self) -> dict:
        return {col: getattr(self, col, "") for col in WALLET_COLUMNS}

    @classmethod
    def from_row(cls, row: dict) -> "WalletSnapshot":
        kwargs = {}
        type_map = {f.name: f.type for f in fields(cls)}
        for col in WALLET_COLUMNS:
            raw = row.get(col, "")
            target = type_map.get(col, str)
            if raw == "" or raw is None:
                kwargs[col] = 0 if target is int else (0.0 if target is float else "")
                continue
            try:
                if target is int:
                    kwargs[col] = int(float(raw))
                elif target is float:
                    kwargs[col] = float(raw)
                else:
                    kwargs[col] = str(raw)
            except (TypeError, ValueError):
                kwargs[col] = raw
        return cls(**kwargs)


class WalletLog:
    """Append-only CSV log of on-chain wallet snapshots."""

    def __init__(self, storage_dir: str = "~/.pod_the_trader") -> None:
        self._storage_dir = Path(storage_dir).expanduser()
        self._path = self._storage_dir / "wallet_snapshots.csv"

    @property
    def path(self) -> Path:
        return self._path

    def append(self, snapshot: WalletSnapshot) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        new_file = not self._path.exists()
        with self._path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=WALLET_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(snapshot.to_row())

    def read_all(self) -> list[WalletSnapshot]:
        if not self._path.exists():
            return []
        with self._path.open(newline="") as f:
            reader = csv.DictReader(f)
            return [WalletSnapshot.from_row(row) for row in reader]

    def latest(self) -> WalletSnapshot | None:
        snapshots = self.read_all()
        return snapshots[-1] if snapshots else None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
