"""Trade ledger: CSV-backed append-only record of every executed swap.

Schema is denormalized and flat for direct spreadsheet import. Every column
needed to compute realized P&L, slippage, gas cost, and entry/exit basis
is stored explicitly so the file is self-contained.
"""

import csv
import json
import logging
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

# Column order — also used as CSV header. Adding new columns at the end
# keeps existing files readable.
LEDGER_COLUMNS = [
    "timestamp",
    "session_id",
    "side",
    "input_mint",
    "input_symbol",
    "input_decimals",
    "input_amount_raw",
    "input_amount_ui",
    "input_price_usd",
    "input_value_usd",
    "output_mint",
    "output_symbol",
    "output_decimals",
    "expected_out_raw",
    "expected_out_ui",
    "actual_out_raw",
    "actual_out_ui",
    "output_price_usd",
    "output_value_usd",
    "slippage_bps_requested",
    "slippage_bps_realized",
    "price_impact_pct",
    "sol_price_usd",
    "gas_lamports",
    "gas_sol",
    "gas_usd",
    "signature",
    "block_slot",
    "block_time",
    "wallet",
    "model",
    "notes",
]


@dataclass
class TradeEntry:
    """Single ledger row. Flat by design — every field is one CSV column."""

    timestamp: str = ""
    session_id: str = ""
    side: str = ""  # "buy" or "sell"
    input_mint: str = ""
    input_symbol: str = ""
    input_decimals: int = 0
    input_amount_raw: int = 0
    input_amount_ui: float = 0.0
    input_price_usd: float = 0.0
    input_value_usd: float = 0.0
    output_mint: str = ""
    output_symbol: str = ""
    output_decimals: int = 0
    expected_out_raw: int = 0
    expected_out_ui: float = 0.0
    actual_out_raw: int = 0
    actual_out_ui: float = 0.0
    output_price_usd: float = 0.0
    output_value_usd: float = 0.0
    slippage_bps_requested: int = 0
    slippage_bps_realized: float = 0.0
    price_impact_pct: float = 0.0
    sol_price_usd: float = 0.0
    gas_lamports: int = 0
    gas_sol: float = 0.0
    gas_usd: float = 0.0
    signature: str = ""
    block_slot: int = 0
    block_time: int = 0
    wallet: str = ""
    model: str = ""
    notes: str = ""

    def to_row(self) -> dict:
        """Return dict in column order for CSV writing."""
        return {col: getattr(self, col, "") for col in LEDGER_COLUMNS}

    @classmethod
    def from_row(cls, row: dict) -> "TradeEntry":
        """Construct from a CSV row dict, coercing types from the field defs."""
        kwargs = {}
        type_map = {f.name: f.type for f in fields(cls)}
        for col in LEDGER_COLUMNS:
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
            return int(float(raw))  # tolerate "1.0" -> 1
        if target_type is float:
            return float(raw)
        return str(raw)
    except (TypeError, ValueError):
        return raw


class TradeLedger:
    """Append-only CSV ledger of trades."""

    def __init__(self, storage_dir: str = "~/.pod_the_trader") -> None:
        self._storage_dir = Path(storage_dir).expanduser()
        self._path = self._storage_dir / "trades.csv"
        self._legacy_json = self._storage_dir / "trade_history.json"

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: TradeEntry) -> None:
        """Append a single trade row to disk."""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        new_file = not self._path.exists()
        with self._path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(entry.to_row())
        logger.info(
            "Recorded trade: %s %s -> %s sig=%s",
            entry.side,
            entry.input_symbol or entry.input_mint[:8],
            entry.output_symbol or entry.output_mint[:8],
            entry.signature[:16],
        )

    def read_all(self) -> list[TradeEntry]:
        """Read all trades from disk. Migrates legacy JSON on first call."""
        if not self._path.exists():
            self._migrate_legacy()
        if not self._path.exists():
            return []
        with self._path.open(newline="") as f:
            reader = csv.DictReader(f)
            return [TradeEntry.from_row(row) for row in reader]

    def _migrate_legacy(self) -> None:
        """One-time import from the old trade_history.json format."""
        if not self._legacy_json.exists():
            return
        try:
            old = json.loads(self._legacy_json.read_text())
        except Exception as e:
            logger.warning("Failed to migrate legacy ledger: %s", e)
            return

        if not old:
            return

        logger.info("Migrating %d legacy trades from JSON to CSV", len(old))
        for t in old:
            entry = TradeEntry(
                timestamp=t.get("timestamp", ""),
                side=t.get("side", ""),
                input_mint=t.get("input_mint", ""),
                input_amount_ui=float(t.get("input_amount", 0)),
                input_value_usd=float(t.get("value_usd", 0)),
                output_mint=t.get("output_mint", ""),
                expected_out_ui=float(t.get("output_amount", 0)),
                actual_out_ui=float(t.get("output_amount", 0)),
                output_price_usd=float(t.get("price_usd", 0)),
                output_value_usd=float(t.get("value_usd", 0)),
                signature=t.get("signature", ""),
                notes="migrated from legacy json",
            )
            self.append(entry)

    def summary(self, since: datetime | None = None) -> dict:
        """Compute P&L summary, optionally filtered to trades since a time."""
        trades = self.read_all()
        if since is not None:
            trades = [
                t for t in trades if _parse_iso(t.timestamp) and _parse_iso(t.timestamp) >= since
            ]

        if not trades:
            return {
                "trade_count": 0,
                "buy_count": 0,
                "sell_count": 0,
                "buy_volume_usd": 0.0,
                "sell_volume_usd": 0.0,
                "gas_spent_usd": 0.0,
                "gas_spent_sol": 0.0,
                "realized_pnl_usd": 0.0,
                "realized_pnl_pct": 0.0,
                "win_rate_pct": 0.0,
                "avg_buy_price": 0.0,
                "avg_sell_price": 0.0,
                "tokens_held": 0.0,
                "first_trade": None,
                "last_trade": None,
            }

        buys = [t for t in trades if t.side == "buy"]
        sells = [t for t in trades if t.side == "sell"]

        buy_volume = sum(t.input_value_usd for t in buys)
        sell_volume = sum(t.output_value_usd for t in sells)
        gas_sol = sum(t.gas_sol for t in trades)
        gas_usd = sum(t.gas_usd for t in trades)

        # Token-weighted average prices
        buy_tokens = sum(t.actual_out_ui for t in buys)
        sell_tokens = sum(t.input_amount_ui for t in sells)
        avg_buy_price = (
            sum(t.actual_out_ui * t.output_price_usd for t in buys) / buy_tokens
            if buy_tokens > 0
            else 0.0
        )
        avg_sell_price = (
            sum(t.input_amount_ui * t.input_price_usd for t in sells) / sell_tokens
            if sell_tokens > 0
            else 0.0
        )

        # Pair buys and sells FIFO for realized PnL
        pairs: list[tuple[float, float]] = []
        buy_queue = list(buys)
        for s in sells:
            if buy_queue:
                b = buy_queue.pop(0)
                pairs.append((b.input_value_usd, s.output_value_usd))

        realized_pnl = sum(out_val - in_val for in_val, out_val in pairs)
        realized_pnl -= gas_usd  # subtract gas
        cost_basis = sum(in_val for in_val, _ in pairs)
        realized_pnl_pct = (realized_pnl / cost_basis * 100) if cost_basis > 0 else 0.0

        wins = sum(1 for in_val, out_val in pairs if out_val > in_val)
        win_rate = (wins / len(pairs) * 100) if pairs else 0.0

        tokens_held = buy_tokens - sell_tokens

        return {
            "trade_count": len(trades),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "buy_volume_usd": buy_volume,
            "sell_volume_usd": sell_volume,
            "gas_spent_usd": gas_usd,
            "gas_spent_sol": gas_sol,
            "realized_pnl_usd": realized_pnl,
            "realized_pnl_pct": realized_pnl_pct,
            "win_rate_pct": win_rate,
            "avg_buy_price": avg_buy_price,
            "avg_sell_price": avg_sell_price,
            "tokens_held": tokens_held,
            "first_trade": trades[0].timestamp if trades else None,
            "last_trade": trades[-1].timestamp if trades else None,
        }

    def __len__(self) -> int:
        return len(self.read_all())

    def per_trade_pnl(self, latest: TradeEntry) -> dict:
        """Compute per-trade P&L for the most recent trade.

        For a BUY: returns entry_price, cost, tokens_acquired, and the new
        position-weighted average entry price.
        For a SELL: pairs against prior unmatched buys FIFO to compute
        realized P&L. Includes gas in the realized number.

        The `latest` entry is assumed to already be in the ledger; the
        method reads the full ledger and treats `latest` as the row being
        reported.
        """
        all_trades = self.read_all()
        # Match by signature (or fall back to last row if blank)
        prior = (
            all_trades[:-1]
            if all_trades and all_trades[-1].signature == latest.signature
            else all_trades
        )

        if latest.side == "buy":
            # All buys including this one to compute new avg entry
            buys = [t for t in all_trades if t.side == "buy"]
            total_tokens = sum(t.actual_out_ui for t in buys)
            total_cost = sum(t.input_value_usd for t in buys)
            avg_entry = total_cost / total_tokens if total_tokens > 0 else 0.0

            entry_price = (
                latest.input_value_usd / latest.actual_out_ui if latest.actual_out_ui > 0 else 0.0
            )
            return {
                "type": "buy",
                "entry_price": entry_price,
                "tokens_acquired": latest.actual_out_ui,
                "cost_usd": latest.input_value_usd,
                "gas_usd": latest.gas_usd,
                "position_avg_entry": avg_entry,
                "position_total_tokens": total_tokens,
                "position_total_cost": total_cost,
            }

        # SELL: pair this sell against prior unmatched buys FIFO
        prior_buys = [t for t in prior if t.side == "buy"]
        prior_sells = [t for t in prior if t.side == "sell"]

        # Subtract amounts already sold from the buy queue
        remaining_buys: list[tuple[float, float, str]] = [
            (b.actual_out_ui, b.input_value_usd, b.timestamp) for b in prior_buys
        ]
        for s in prior_sells:
            sold = s.input_amount_ui
            while sold > 0 and remaining_buys:
                tokens, cost, ts = remaining_buys[0]
                if tokens <= sold:
                    sold -= tokens
                    remaining_buys.pop(0)
                else:
                    portion = sold / tokens
                    remaining_buys[0] = (
                        tokens - sold,
                        cost * (1 - portion),
                        ts,
                    )
                    sold = 0

        # Now match the current sell against remaining_buys FIFO
        sell_tokens = latest.input_amount_ui
        sell_proceeds = latest.output_value_usd
        matched_cost = 0.0
        matched_tokens = 0.0
        weighted_entry_num = 0.0

        sell_left = sell_tokens
        for tokens, cost, _ts in list(remaining_buys):
            if sell_left <= 0:
                break
            if tokens <= sell_left:
                matched_cost += cost
                matched_tokens += tokens
                weighted_entry_num += cost  # cost / tokens * tokens = cost
                sell_left -= tokens
            else:
                portion = sell_left / tokens
                lot_cost = cost * portion
                matched_cost += lot_cost
                matched_tokens += sell_left
                weighted_entry_num += lot_cost
                sell_left = 0

        # Proceeds for the matched portion (if sell partially exceeds buys)
        matched_proceeds = (
            sell_proceeds * (matched_tokens / sell_tokens) if sell_tokens > 0 else 0.0
        )

        avg_entry = weighted_entry_num / matched_tokens if matched_tokens > 0 else 0.0
        exit_price = sell_proceeds / sell_tokens if sell_tokens > 0 else 0.0
        realized = matched_proceeds - matched_cost - latest.gas_usd
        realized_pct = (realized / matched_cost * 100) if matched_cost > 0 else 0.0

        return {
            "type": "sell",
            "entry_price": avg_entry,
            "exit_price": exit_price,
            "tokens_sold": sell_tokens,
            "tokens_matched": matched_tokens,
            "cost_basis_usd": matched_cost,
            "proceeds_usd": matched_proceeds,
            "gas_usd": latest.gas_usd,
            "realized_pnl_usd": realized,
            "realized_pnl_pct": realized_pct,
            "unmatched_tokens": max(0.0, sell_tokens - matched_tokens),
        }


def format_trade_pnl(pnl: dict) -> str:
    """Format a per_trade_pnl dict as a multi-line summary string."""
    if pnl["type"] == "buy":
        lines = [
            f"[TRADE BUY] +{pnl['tokens_acquired']:.6f} tokens @ ${pnl['entry_price']:.8f}",
            f"  cost:        ${pnl['cost_usd']:.4f}",
            f"  gas:         ${pnl['gas_usd']:.4f}",
            f"  position:    {pnl['position_total_tokens']:.4f} tokens "
            f"@ avg ${pnl['position_avg_entry']:.8f} "
            f"(${pnl['position_total_cost']:.4f} total cost)",
        ]
        return "\n".join(lines)

    sign = "+" if pnl["realized_pnl_usd"] >= 0 else ""
    lines = [
        f"[TRADE SELL] -{pnl['tokens_sold']:.6f} tokens @ ${pnl['exit_price']:.8f}",
        f"  entry:       ${pnl['entry_price']:.8f}",
        f"  exit:        ${pnl['exit_price']:.8f}",
        f"  cost basis:  ${pnl['cost_basis_usd']:.4f}",
        f"  proceeds:    ${pnl['proceeds_usd']:.4f}",
        f"  gas:         ${pnl['gas_usd']:.4f}",
        f"  REALIZED:    {sign}${pnl['realized_pnl_usd']:.4f} "
        f"({sign}{pnl['realized_pnl_pct']:.2f}%)",
    ]
    if pnl.get("unmatched_tokens", 0) > 0:
        lines.append(f"  unmatched:   {pnl['unmatched_tokens']:.6f} tokens (no prior buy to match)")
    return "\n".join(lines)


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp, returning None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_iso() -> str:
    """Current UTC time as an ISO string."""
    return datetime.now(UTC).isoformat()
