"""Trade ledger panel — a scrollable DataTable of recent trades."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from textual.widgets import DataTable

if TYPE_CHECKING:
    from pod_the_trader.data.ledger import TradeEntry, TradeLedger

logger = logging.getLogger(__name__)

# Solana base58 transaction signatures are 87–88 characters long.
# Cap the on-screen sig width here so the column never wastes width
# on padding past the natural maximum, even on a very wide terminal.
SOL_SIG_MAX_LEN = 88

# Block explorer the bot links to when the operator clicks a row.
ORB_EXPLORER_TX_BASE = "https://orbmarkets.io/tx/"


class LedgerWidget(DataTable):
    """A DataTable of recent trades, newest-first.

    Extends DataTable directly so it renders cleanly as a leaf widget —
    the title is provided via the panel border in app.py CSS. The
    signature column expands to use whatever horizontal space the
    panel has available (truncated with an ellipsis when narrow,
    showing the full signature when wide), and clicking a row opens
    the matching transaction on orbmarkets.io.
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
        # Full signatures, indexed by displayed row (newest first), so
        # a row-click can open the full transaction on the explorer
        # regardless of how truncated the on-screen sig is.
        self._row_sigs: list[str] = []
        # Cache the last computed sig-column budget so on_resize only
        # rebuilds rows when the budget actually changed (resizing the
        # vertical axis doesn't churn the table).
        self._last_sig_width: int = 0

    def on_mount(self) -> None:
        self.add_columns("#", "time", "side", "tokens", "$ value", "sig")
        self._last_sig_width = self._sig_width_budget()
        self.refresh_rows()

    def on_resize(self) -> None:
        new_width = self._sig_width_budget()
        if new_width != self._last_sig_width:
            self._last_sig_width = new_width
            self.refresh_rows()

    def refresh_rows(self) -> None:
        if self._ledger is None:
            return
        self.clear()
        self._row_sigs = []
        trades = self._ledger.read_all()
        sig_width = self._last_sig_width or self._sig_width_budget()
        # newest first
        for i, t in enumerate(reversed(trades), start=1):
            n = len(trades) - i + 1
            row, full_sig = _format_trade_row(i, n, t, sig_width)
            self.add_row(*row)
            self._row_sigs.append(full_sig)

    def add_trade(self, entry: TradeEntry) -> None:
        """Append a single new trade to the top of the table.

        Keeps the ``_row_sigs`` lookup in sync so a click on the newly
        added row opens the right transaction. Existing rows are not
        rewritten — their on-screen sig column doesn't grow when the
        table gets a new row, which is consistent with how DataTable
        treats existing cells.
        """
        if self._ledger is None:
            return
        count = len(self._ledger.read_all())
        sig_width = self._last_sig_width or self._sig_width_budget()
        row, full_sig = _format_trade_row(1, count, entry, sig_width)
        self.add_row(*row)
        # New rows go at the top of the visual table (newest first),
        # so prepend to the sig lookup to keep indices aligned.
        self._row_sigs.insert(0, full_sig)

    def _sig_width_budget(self) -> int:
        """Compute the number of characters the sig column gets.

        Subtracts the fixed-content widths of the other columns plus
        DataTable's inter-column padding from the widget's current
        width. Returns at least 8 (so the truncated signature always
        carries enough of the prefix to be recognizable) and at most
        ``SOL_SIG_MAX_LEN`` (no point reserving width past a full
        Solana signature). When the widget hasn't been laid out yet
        (``self.size.width == 0``) returns a reasonable default that
        the first resize will replace.
        """
        widget_w = self.size.width
        if widget_w <= 0:
            return 12  # narrow default until the first resize
        # Approximate fixed-column content widths plus per-column
        # padding/spacing. DataTable adds ~1 char of separator
        # between adjacent columns (5 separators for 6 columns).
        fixed = 3 + 8 + 4 + 14 + 10 + 5  # # + time + side + tokens + $value + paddings
        budget = widget_w - fixed
        return max(8, min(SOL_SIG_MAX_LEN, budget))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the clicked trade's transaction on orbmarkets.io.

        Triggered by both mouse click and Enter when the cursor is on
        a row (DataTable emits ``RowSelected`` for both). Looks up the
        full signature via the cursor row index and shells out to the
        default browser. Silently no-ops if the row has no signature
        (e.g. a synthetic row, or a future row type without a
        signature field).
        """
        row = event.cursor_row
        if not (0 <= row < len(self._row_sigs)):
            return
        sig = self._row_sigs[row]
        if not sig:
            return
        url = f"{ORB_EXPLORER_TX_BASE}{sig}"
        try:
            import webbrowser

            webbrowser.open(url)
            with contextlib.suppress(Exception):
                self.app.notify(
                    f"Opening tx {sig[:8]}… on orbmarkets.io",
                    title="Transaction",
                    timeout=2,
                )
        except Exception as e:
            logger.warning("Failed to open transaction URL %s: %s", url, e)


def _format_trade_row(
    display_idx: int,
    n: int,
    t: TradeEntry,
    sig_width: int,
) -> tuple[tuple[str, ...], str]:
    """Render a single ledger row plus the full signature.

    Returned tuple is ``(row_cells, full_signature)`` — the cells get
    fed to ``DataTable.add_row`` and the full signature is stored on
    the widget so a row-click can open the correct explorer URL even
    when the on-screen ``sig`` cell is truncated.
    """
    ts = t.timestamp
    short_time = ts[11:19] if len(ts) > 19 else ts  # HH:MM:SS from ISO
    side = t.side.upper()
    side_color = "#00ff88" if side == "BUY" else ("#ff3366" if side == "SELL" else "#556677")

    tokens = t.actual_out_ui if side == "BUY" else t.input_amount_ui
    value = t.input_value_usd if side == "BUY" else t.output_value_usd

    full_sig = t.signature or ""
    sig_display = _truncate_sig(full_sig, sig_width)

    return (
        (
            f"{n}",
            short_time,
            f"[{side_color}]{side}[/]",
            f"{tokens:,.2f}",
            f"${value:,.2f}",
            sig_display,
        ),
        full_sig,
    )


def _truncate_sig(sig: str, width: int) -> str:
    """Return ``sig`` clipped to ``width`` chars, with a trailing `…`
    when actually truncated. Returns ``""`` for an empty signature
    (synthetic rows, recovery placeholders, etc.) so the column reads
    blank rather than showing a lone ellipsis.
    """
    if not sig:
        return ""
    if len(sig) <= width:
        return sig
    # Reserve one slot for the ellipsis so the visible width still
    # matches the budget. width<=1 produces just the ellipsis.
    prefix_len = max(0, width - 1)
    return sig[:prefix_len] + "…"
