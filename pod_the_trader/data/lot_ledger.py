"""Lot-based cost-basis ledger: event-sourced record of every position change.

Unlike ``TradeLedger`` (which records only bot-initiated swaps), this ledger
is the authoritative model for **what the wallet owns and at what cost basis**.
Every event that changes a position — bot trade, external deposit, external
withdrawal, gas payment — is an append-only row in ``lot_events.csv``.

## Data model

Each row is a ``LotEvent``. Events come in two flavors:

* ``open`` adds a lot of ``qty`` units with a cost basis of ``unit_price_usd``
  each. Opens happen when the bot buys a token, or when the reconciler notices
  a positive on-chain delta it can't attribute to a bot trade (an external
  deposit or incoming swap).
* ``close`` consumes prior open lots FIFO. The ``unit_price_usd`` of a close
  event is the *proceeds per unit* — what we received (or valued the outflow
  at) when the tokens left. A close produces one or more ``ClosedSegment``
  entries at replay time, which carry both the entry (cost basis) and exit
  (proceeds) price for the consumed portion of each open lot.

Both kinds carry a ``source``:

* ``trade`` — a bot-initiated swap (both the sold side and the bought side).
  Realized P&L from these closes is the number users think of as "trading P&L".
* ``reconcile`` — a synthetic event emitted by the reconciler after it
  compared on-chain balances to the ledger's open-lot sum. Realized P&L on
  reconcile closes is **not** counted as trading P&L (we don't know if the
  tokens were actually sold or just transferred out).
* ``gas`` — SOL spent on transaction fees during a bot swap. Tracked so the
  reconciler doesn't flag the deducted SOL as an external withdrawal.

## P&L math

Realized P&L is computed from ``ClosedSegment`` entries: for each segment,
``(exit_price - entry_price) * qty``. Filter by source to get trading P&L
specifically. Unrealized P&L uses the remaining open lots: ``current_price *
open_qty - sum(remaining_qty * cost_basis)``. Total P&L is the sum.

## Persistence

Events are append-only CSV rows. ``replay()`` rebuilds the full position state
(open lots + closed segments per mint) by walking the file top-to-bottom.
Cheap enough for the scales this bot operates at; revisit if the file grows
past tens of thousands of rows.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------

LOT_COLUMNS = [
    "timestamp",
    "mint",
    "kind",  # "open" | "close"
    "qty",  # always positive
    "unit_price_usd",  # cost basis (open) or proceeds price (close), per unit
    "source",  # "trade" | "reconcile" | "gas"
    "ref_sig",  # optional reference: tx signature for trades, "" otherwise
    "notes",
]

KIND_OPEN = "open"
KIND_CLOSE = "close"

SOURCE_TRADE = "trade"
SOURCE_RECONCILE = "reconcile"
SOURCE_GAS = "gas"

VALID_KINDS = {KIND_OPEN, KIND_CLOSE}
VALID_SOURCES = {SOURCE_TRADE, SOURCE_RECONCILE, SOURCE_GAS}


@dataclass
class LotEvent:
    """Single row in the lot ledger. Append-only, flat for CSV round-trip."""

    timestamp: str = ""
    mint: str = ""
    kind: str = ""
    qty: float = 0.0
    unit_price_usd: float = 0.0
    source: str = ""
    ref_sig: str = ""
    notes: str = ""

    def to_row(self) -> dict:
        return {col: getattr(self, col, "") for col in LOT_COLUMNS}

    @classmethod
    def from_row(cls, row: dict) -> LotEvent:
        kwargs: dict = {}
        type_map = {f.name: f.type for f in fields(cls)}
        for col in LOT_COLUMNS:
            raw = row.get(col, "")
            target = type_map.get(col, str)
            kwargs[col] = _coerce(raw, target)
        return cls(**kwargs)


def _coerce(raw: str, target_type: object) -> object:
    # ``from __future__ import annotations`` stringifies type hints, so
    # ``field.type`` is "int" / "float" / "str" rather than the type object.
    name = target_type if isinstance(target_type, str) else getattr(target_type, "__name__", "str")
    if raw == "" or raw is None:
        if name == "int":
            return 0
        if name == "float":
            return 0.0
        return ""
    try:
        if name == "int":
            return int(float(raw))
        if name == "float":
            return float(raw)
        return str(raw)
    except (TypeError, ValueError):
        return raw


# ---------------------------------------------------------------------------
# Replayed position state
# ---------------------------------------------------------------------------


@dataclass
class OpenLot:
    """An open lot with its remaining (un-closed) quantity."""

    event: LotEvent
    remaining_qty: float


@dataclass
class ClosedSegment:
    """One matched (entry, exit) segment produced when a close consumed part
    of an open lot.

    Realized P&L for this segment is ``(exit_price - entry_price) * qty``.
    """

    qty: float
    entry_price: float
    exit_price: float
    entry_ts: str
    exit_ts: str
    source: str  # mirrors the close event's source
    ref_sig: str


@dataclass
class PositionState:
    """Replayed state for a single mint."""

    mint: str
    open_lots: list[OpenLot] = field(default_factory=list)
    closed_segments: list[ClosedSegment] = field(default_factory=list)

    @property
    def open_qty(self) -> float:
        return sum(lot.remaining_qty for lot in self.open_lots)

    @property
    def cost_basis_usd(self) -> float:
        return sum(lot.remaining_qty * lot.event.unit_price_usd for lot in self.open_lots)

    @property
    def avg_cost_basis(self) -> float:
        q = self.open_qty
        return (self.cost_basis_usd / q) if q > 0 else 0.0

    def realized_pnl(self, sources: tuple[str, ...] = (SOURCE_TRADE,)) -> float:
        """Sum ``(exit - entry) * qty`` across matched closes, filtered by source."""
        return sum(
            (seg.exit_price - seg.entry_price) * seg.qty
            for seg in self.closed_segments
            if seg.source in sources
        )

    def unrealized_pnl(self, current_price_usd: float) -> float:
        return current_price_usd * self.open_qty - self.cost_basis_usd

    def total_pnl(
        self,
        current_price_usd: float,
        sources: tuple[str, ...] = (SOURCE_TRADE,),
    ) -> float:
        return self.realized_pnl(sources) + self.unrealized_pnl(current_price_usd)

    def gas_usd(self) -> float:
        """Total USD value of SOL closed as ``gas`` (entry_price * qty)."""
        return sum(
            seg.entry_price * seg.qty for seg in self.closed_segments if seg.source == SOURCE_GAS
        )

    def trade_close_count(self) -> int:
        """Number of trade-sourced close events (merged adjacent segments)."""
        # Segments are produced in order; group by exit_ts+ref_sig to count
        # distinct close events instead of FIFO sub-segments.
        seen: set[tuple[str, str]] = set()
        for seg in self.closed_segments:
            if seg.source != SOURCE_TRADE:
                continue
            key = (seg.exit_ts, seg.ref_sig)
            seen.add(key)
        return len(seen)

    def win_rate_pct(self) -> float:
        """Pct of trade-sourced closes that produced positive realized P&L.

        A close can be split across multiple FIFO segments; aggregate by
        ``(exit_ts, ref_sig)`` so the win rate counts whole trades, not
        sub-segments.
        """
        grouped: dict[tuple[str, str], float] = {}
        for seg in self.closed_segments:
            if seg.source != SOURCE_TRADE:
                continue
            key = (seg.exit_ts, seg.ref_sig)
            grouped[key] = grouped.get(key, 0.0) + (seg.exit_price - seg.entry_price) * seg.qty
        if not grouped:
            return 0.0
        wins = sum(1 for pnl in grouped.values() if pnl > 0)
        return wins / len(grouped) * 100


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class LotLedger:
    """Append-only lot event ledger with on-demand replay."""

    def __init__(self, storage_dir: str = "~/.pod_the_trader") -> None:
        self._storage_dir = Path(storage_dir).expanduser()
        self._path = self._storage_dir / "lot_events.csv"

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def event_count(self) -> int:
        return len(self.read_all())

    def append(self, event: LotEvent) -> None:
        """Append a single event row to disk."""
        if event.kind not in VALID_KINDS:
            raise ValueError(f"Invalid kind: {event.kind!r}")
        if event.source not in VALID_SOURCES:
            raise ValueError(f"Invalid source: {event.source!r}")
        if event.qty <= 0:
            raise ValueError(f"Event qty must be positive, got {event.qty}")
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        new_file = not self._path.exists()
        with self._path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOT_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(event.to_row())
        logger.debug(
            "Lot event: %s %s %.6f @ $%.8f (%s)",
            event.kind,
            event.mint[:8],
            event.qty,
            event.unit_price_usd,
            event.source,
        )

    def append_many(self, events: list[LotEvent]) -> None:
        for e in events:
            self.append(e)

    def read_all(self) -> list[LotEvent]:
        if not self._path.exists():
            return []
        with self._path.open(newline="") as f:
            reader = csv.DictReader(f)
            return [LotEvent.from_row(row) for row in reader]

    # ---- Replay ----

    def replay(self) -> dict[str, PositionState]:
        """Walk events in order and build per-mint ``PositionState``."""
        states: dict[str, PositionState] = {}
        for ev in self.read_all():
            state = states.setdefault(ev.mint, PositionState(mint=ev.mint))
            if ev.kind == KIND_OPEN:
                state.open_lots.append(OpenLot(event=ev, remaining_qty=ev.qty))
            elif ev.kind == KIND_CLOSE:
                _consume_fifo(state, ev)
            else:
                logger.warning("Unknown lot event kind: %s", ev.kind)
        return states

    def position_state(self, mint: str) -> PositionState:
        return self.replay().get(mint, PositionState(mint=mint))

    def open_qty(self, mint: str) -> float:
        return self.position_state(mint).open_qty

    def summary(self, mint: str, current_price_usd: float) -> dict:
        """Return a display-ready summary dict for one mint."""
        state = self.position_state(mint)
        pos_value = state.open_qty * current_price_usd
        realized = state.realized_pnl()
        unrealized = state.unrealized_pnl(current_price_usd)
        # Percent is realized against total cost basis of consumed lots.
        consumed_basis = sum(
            seg.entry_price * seg.qty for seg in state.closed_segments if seg.source == SOURCE_TRADE
        )
        realized_pct = (realized / consumed_basis * 100) if consumed_basis > 0 else 0.0
        return {
            "mint": mint,
            "open_qty": state.open_qty,
            "cost_basis_usd": state.cost_basis_usd,
            "avg_cost_basis": state.avg_cost_basis,
            "position_value_usd": pos_value,
            "realized_pnl_usd": realized,
            "realized_pnl_pct": realized_pct,
            "unrealized_pnl_usd": unrealized,
            "total_pnl_usd": realized + unrealized,
            "gas_usd": state.gas_usd(),
            "trade_close_count": state.trade_close_count(),
            "trade_count": state.trade_close_count(),  # alias for TUI widget
            "win_rate_pct": state.win_rate_pct(),
            "open_lot_count": len(state.open_lots),
        }


def _consume_fifo(state: PositionState, close: LotEvent) -> None:
    """Consume ``close.qty`` units from the head of ``state.open_lots`` FIFO,
    producing one or more ``ClosedSegment`` entries.

    If the close exceeds available open quantity, the excess is dropped with
    a warning. This can happen when the reconciler sees a drop larger than
    the tracked basis (e.g. first-ever reconciliation against a wallet that
    already held tokens before the ledger existed).
    """
    remaining = close.qty
    while remaining > 1e-12 and state.open_lots:
        head = state.open_lots[0]
        take = min(head.remaining_qty, remaining)
        state.closed_segments.append(
            ClosedSegment(
                qty=take,
                entry_price=head.event.unit_price_usd,
                exit_price=close.unit_price_usd,
                entry_ts=head.event.timestamp,
                exit_ts=close.timestamp,
                source=close.source,
                ref_sig=close.ref_sig,
            )
        )
        head.remaining_qty -= take
        remaining -= take
        if head.remaining_qty <= 1e-12:
            state.open_lots.pop(0)

    if remaining > 1e-9:
        # Logged at DEBUG because replay() runs many times per cycle and we
        # don't want a 60-line warning storm. Migrated histories often have
        # closes without prior basis (the user held tokens before the bot
        # started tracking); the startup reconciler resolves the position
        # against on-chain truth so the lingering "unmatched" amount is
        # informational, not actionable.
        logger.debug(
            "Close event exceeded open qty for %s: %.6f unmatched (source=%s, sig=%s)",
            close.mint[:8],
            remaining,
            close.source,
            close.ref_sig[:16],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def migrate_from_trade_ledger(
    lot_ledger: LotLedger,
    trade_entries: list,
    *,
    sol_mint: str,
) -> int:
    """One-time import: replay a legacy ``TradeLedger`` into lot events.

    Migration is **target-token-only** — the SOL leg of each historical
    swap is intentionally skipped. Why: a legacy trade ledger only knows
    about bot-initiated swaps, so its implied SOL position bears no
    relation to what's actually on-chain (the wallet was funded externally,
    SOL flowed in and out for non-trade reasons, etc.). Replaying the SOL
    legs would inflate SOL open lots with phantom basis from sell proceeds
    and produce nonsense P&L.

    Instead, we only replay the token leg — opens for buys, closes for
    sells — which gives a roughly correct token cost basis. The startup
    reconciler immediately follows up by absorbing actual on-chain SOL +
    token balances against the freshly migrated state, fixing any drift.

    Skips if ``lot_ledger`` already has events (idempotent). Returns the
    number of events emitted. Accepts ``trade_entries`` as a plain list so
    this module doesn't need to import ``TradeLedger`` (which would create
    a dependency cycle).
    """
    if lot_ledger.event_count() > 0:
        return 0

    count = 0
    for entry in trade_entries:
        side = getattr(entry, "side", "")
        timestamp = getattr(entry, "timestamp", "") or now_iso()
        sig = getattr(entry, "signature", "") or ""

        if side == "buy":
            # Token came in: open a token lot at the swap's price-per-token.
            output_mint = getattr(entry, "output_mint", "")
            output_qty = float(getattr(entry, "actual_out_ui", 0) or 0)
            output_price = float(getattr(entry, "output_price_usd", 0) or 0)
            if output_mint and output_mint != sol_mint and output_qty > 0:
                ev = LotEvent(
                    timestamp=timestamp,
                    mint=output_mint,
                    kind=KIND_OPEN,
                    qty=output_qty,
                    unit_price_usd=output_price,
                    source=SOURCE_TRADE,
                    ref_sig=sig,
                    notes="migrated buy",
                )
                try:
                    lot_ledger.append(ev)
                    count += 1
                except ValueError as e:
                    logger.warning("Skipped invalid migrated event: %s", e)

        elif side == "sell":
            # Token went out: close token lots at the swap's price-per-token.
            input_mint = getattr(entry, "input_mint", "")
            input_qty = float(getattr(entry, "input_amount_ui", 0) or 0)
            input_price = float(getattr(entry, "input_price_usd", 0) or 0)
            if input_mint and input_mint != sol_mint and input_qty > 0:
                ev = LotEvent(
                    timestamp=timestamp,
                    mint=input_mint,
                    kind=KIND_CLOSE,
                    qty=input_qty,
                    unit_price_usd=input_price,
                    source=SOURCE_TRADE,
                    ref_sig=sig,
                    notes="migrated sell",
                )
                try:
                    lot_ledger.append(ev)
                    count += 1
                except ValueError as e:
                    logger.warning("Skipped invalid migrated event: %s", e)

    if count:
        logger.info(
            "Migrated %d token-leg lot events from legacy trade ledger "
            "(SOL legs skipped — reconciler will absorb actual on-chain SOL)",
            count,
        )
    return count


def emit_trade_events(
    *,
    timestamp: str,
    input_mint: str,
    input_qty: float,
    input_price_usd: float,
    output_mint: str,
    output_qty: float,
    output_price_usd: float,
    gas_sol: float,
    sol_price_usd: float,
    sol_mint: str,
    tx_sig: str = "",
) -> list[LotEvent]:
    """Build the lot events for one successful bot swap.

    Produces up to three events:
      1. close(input_mint, input_qty) @ input_price  — the tokens we gave up
      2. open(output_mint, output_qty) @ output_price — the tokens we got
      3. close(sol_mint, gas_sol) @ sol_price source=gas — the network fee
    """
    events: list[LotEvent] = []
    if input_qty > 0:
        events.append(
            LotEvent(
                timestamp=timestamp,
                mint=input_mint,
                kind=KIND_CLOSE,
                qty=input_qty,
                unit_price_usd=input_price_usd,
                source=SOURCE_TRADE,
                ref_sig=tx_sig,
            )
        )
    if output_qty > 0:
        events.append(
            LotEvent(
                timestamp=timestamp,
                mint=output_mint,
                kind=KIND_OPEN,
                qty=output_qty,
                unit_price_usd=output_price_usd,
                source=SOURCE_TRADE,
                ref_sig=tx_sig,
            )
        )
    if gas_sol > 0:
        events.append(
            LotEvent(
                timestamp=timestamp,
                mint=sol_mint,
                kind=KIND_CLOSE,
                qty=gas_sol,
                unit_price_usd=sol_price_usd,
                source=SOURCE_GAS,
                ref_sig=tx_sig,
            )
        )
    return events
