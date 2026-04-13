"""Price log: CSV time series for SOL + target token, sampled each cycle.

Provides enough raw price data to compute returns, volatility, drawdowns,
correlations, beta, and any other quant metric. One row per (mint, sample).
"""

import csv
import logging
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PRICE_COLUMNS = [
    "timestamp",
    "mint",
    "symbol",
    "price_usd",
    "liquidity_usd",
    "price_change_24h_pct",
    "block_id",
    "decimals",
    "source",
]


@dataclass
class PriceTick:
    timestamp: str = ""
    mint: str = ""
    symbol: str = ""
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    price_change_24h_pct: float = 0.0
    block_id: int = 0
    decimals: int = 0
    source: str = ""

    def to_row(self) -> dict:
        return {col: getattr(self, col, "") for col in PRICE_COLUMNS}

    @classmethod
    def from_row(cls, row: dict) -> "PriceTick":
        kwargs = {}
        type_map = {f.name: f.type for f in fields(cls)}
        for col in PRICE_COLUMNS:
            raw = row.get(col, "")
            target_type = type_map.get(col, str)
            kwargs[col] = _coerce(raw, target_type)
        return cls(**kwargs)


def _coerce(raw: str, target_type) -> object:
    if raw == "" or raw is None:
        if target_type is int:
            return 0
        if target_type is float:
            return 0.0
        return ""
    try:
        if target_type is int:
            return int(float(raw))
        if target_type is float:
            return float(raw)
        return str(raw)
    except (TypeError, ValueError):
        return raw


class PriceLog:
    """Append-only CSV time series of token prices."""

    def __init__(self, storage_dir: str = "~/.pod_the_trader") -> None:
        self._storage_dir = Path(storage_dir).expanduser()
        self._path = self._storage_dir / "prices.csv"

    @property
    def path(self) -> Path:
        return self._path

    def append(self, tick: PriceTick) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        new_file = not self._path.exists()
        with self._path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=PRICE_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(tick.to_row())

    def append_many(self, ticks: list[PriceTick]) -> None:
        for tick in ticks:
            self.append(tick)

    def read_all(self) -> list[PriceTick]:
        if not self._path.exists():
            return []
        with self._path.open(newline="") as f:
            reader = csv.DictReader(f)
            return [PriceTick.from_row(row) for row in reader]

    def read_for_mint(self, mint: str) -> list[PriceTick]:
        return [t for t in self.read_all() if t.mint == mint]

    def latest(self, mint: str) -> PriceTick | None:
        ticks = self.read_for_mint(mint)
        return ticks[-1] if ticks else None

    def returns(self, mint: str) -> list[float]:
        """Period-over-period log returns for a mint."""
        ticks = self.read_for_mint(mint)
        prices = [t.price_usd for t in ticks if t.price_usd > 0]
        if len(prices) < 2:
            return []
        import math

        return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]

    def volatility(self, mint: str) -> float:
        """Sample standard deviation of period log returns."""
        rets = self.returns(mint)
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return var**0.5

    def __len__(self) -> int:
        return len(self.read_all())


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
