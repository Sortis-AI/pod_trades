"""Trade ledger panel — a scrollable DataTable of recent trades."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import DataTable

if TYPE_CHECKING:
    from pod_the_trader.data.ledger import TradeEntry, TradeLedger


class LedgerWidget(DataTable):
    """A DataTable of recent trades, newest-first.

    Extends DataTable directly so it renders cleanly as a leaf widget —
    the title is provided via the panel border in app.py CSS.
    """

    DEFAULT_CSS = """
    LedgerWidget {
        height: 1fr;
        background: #0a0f1e;
    }
    """

    def __init__(self, ledger: TradeLedger | None = None, **kwargs) -> None:
        super().__init__(
            zebra_stripes=False,
            show_cursor=True,
            cursor_type="row",
            **kwargs,
        )
        self._ledger = ledger

    def on_mount(self) -> None:
        self.add_columns("#", "time", "side", "tokens", "$ value", "sig")
        self.refresh_rows()

    def refresh_rows(self) -> None:
        if self._ledger is None:
            return
        self.clear()
        trades = self._ledger.read_all()
        # newest first
        for i, t in enumerate(reversed(trades), start=1):
            self.add_row(*_format_trade_row(i, len(trades) - i + 1, t))

    def add_trade(self, entry: TradeEntry) -> None:
        """Append a single new trade to the top of the table."""
        if self._ledger is None:
            return
        count = len(self._ledger.read_all())
        self.add_row(*_format_trade_row(1, count, entry))


def _format_trade_row(display_idx: int, n: int, t: TradeEntry) -> tuple[str, ...]:
    ts = t.timestamp
    short_time = ts[11:19] if len(ts) > 19 else ts  # HH:MM:SS from ISO
    side = t.side.upper()
    side_color = "#00ff88" if side == "BUY" else ("#ff3366" if side == "SELL" else "#556677")

    tokens = t.actual_out_ui if side == "BUY" else t.input_amount_ui
    value = t.input_value_usd if side == "BUY" else t.output_value_usd
    sig = (t.signature or "")[:8] + ("…" if t.signature else "")

    return (
        f"{n}",
        short_time,
        f"[{side_color}]{side}[/]",
        f"{tokens:,.2f}",
        f"${value:,.2f}",
        sig,
    )
