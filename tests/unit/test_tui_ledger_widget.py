"""Tests for the trade-ledger widget — signature column expansion + click."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from textual.app import App, ComposeResult

if TYPE_CHECKING:
    from pathlib import Path

from pod_the_trader.data.ledger import TradeEntry, TradeLedger
from pod_the_trader.data.price_log import now_iso
from pod_the_trader.tui.widgets.ledger import (
    ORB_EXPLORER_TX_BASE,
    LedgerWidget,
    _truncate_sig,
)

SAMPLE_SIG = (
    "5xQv9aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890abcdefghij"
    "klmnopqrstuvwxyzABCDEFGHIJKLMNOP"
)  # 82-ish chars, representative of a real base58 signature


@pytest.fixture()
def trade(tmp_path: Path) -> TradeEntry:
    return TradeEntry(
        timestamp=now_iso(),
        side="buy",
        input_mint="So11111111111111111111111111111111111111112",
        input_symbol="SOL",
        input_decimals=9,
        input_amount_raw=100_000_000,
        input_amount_ui=0.1,
        input_value_usd=15.0,
        output_mint="EN2nnxrg8uUi6x2sJkzNPd2eT6rB9rdSoQNNaENA4RZA",
        output_decimals=6,
        expected_out_raw=50_000_000,
        expected_out_ui=50.0,
        actual_out_raw=49_500_000,
        actual_out_ui=49.5,
        output_price_usd=0.3,
        output_value_usd=14.85,
        signature=SAMPLE_SIG,
    )


@pytest.fixture()
def ledger_with_trade(tmp_path: Path, trade: TradeEntry) -> TradeLedger:
    ledger = TradeLedger(storage_dir=str(tmp_path))
    ledger.append(trade)
    return ledger


class _LedgerHarness(App):
    """Mounts a single LedgerWidget so its reactive state can be
    driven and inspected through Textual's virtual-terminal harness.
    """

    def __init__(self, ledger: TradeLedger) -> None:
        super().__init__()
        self._ledger = ledger

    def compose(self) -> ComposeResult:
        yield LedgerWidget(self._ledger, id="ledger")

    @property
    def widget(self) -> LedgerWidget:
        return self.query_one("#ledger", LedgerWidget)


class TestTruncateSig:
    def test_empty_returns_empty(self) -> None:
        assert _truncate_sig("", 50) == ""

    def test_fits_returns_full(self) -> None:
        assert _truncate_sig("abcd1234", 10) == "abcd1234"

    def test_exactly_fits_returns_full(self) -> None:
        # width == len → no ellipsis, full string visible.
        assert _truncate_sig("abcdefgh", 8) == "abcdefgh"

    def test_truncates_with_ellipsis_when_too_long(self) -> None:
        out = _truncate_sig("0123456789", 6)
        assert out.endswith("…")
        assert len(out) == 6
        assert out == "01234…"

    def test_full_solana_sig_fits_at_max_width(self) -> None:
        # 88-char sig fits exactly in the 88-char max budget.
        sig = "x" * 88
        assert _truncate_sig(sig, 88) == sig


class TestSignatureColumnExpansion:
    async def test_narrow_widget_truncates_signature(self, ledger_with_trade: TradeLedger) -> None:
        # A small terminal forces the sig column down to its minimum
        # budget; the displayed cell ends in an ellipsis.
        app = _LedgerHarness(ledger_with_trade)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            widget = app.widget
            # Pull the sig cell (column 6) of the first row.
            cell = widget.get_cell_at((0, 6))
            assert cell.endswith("…")
            # And the stored full signature is the untruncated original
            # so a click still opens the correct URL.
            assert widget._row_sigs == [SAMPLE_SIG]

    async def test_wide_widget_shows_full_signature(self, ledger_with_trade: TradeLedger) -> None:
        # A wide terminal gives the sig column enough budget to show
        # the entire 82-char signature without truncation.
        app = _LedgerHarness(ledger_with_trade)
        async with app.run_test(size=(200, 24)) as pilot:
            await pilot.pause()
            widget = app.widget
            cell = widget.get_cell_at((0, 6))
            assert cell == SAMPLE_SIG
            assert not cell.endswith("…")


class TestTotalWidthFitsWidget:
    """The reason _sig_width_budget measures the other columns instead
    of using a static estimate: at narrow widths, an undercounted
    fixed-column total pushed the sig past the panel edge and Textual
    surfaced a horizontal scrollbar across the entire row. Verify
    that the sum of every column's rendered width is ≤ the widget
    width across a representative spread of terminal sizes.
    """

    @pytest.mark.parametrize("term_width", [80, 100, 120, 160, 200, 240])
    async def test_no_horizontal_overflow_at_size(
        self, ledger_with_trade: TradeLedger, term_width: int
    ) -> None:
        app = _LedgerHarness(ledger_with_trade)
        async with app.run_test(size=(term_width, 24)) as pilot:
            await pilot.pause()
            widget = app.widget
            total = sum(col.get_render_width(widget) for col in widget.columns.values())
            # Sig overhead in the budget gives us a small slack; total
            # rendered width must never exceed the widget's available
            # width (which == panel width minus border + padding).
            assert total <= widget.size.width, (
                f"Total column width {total} exceeds widget width "
                f"{widget.size.width} at terminal width {term_width}"
            )


class TestRowClickOpensExplorer:
    async def test_row_selected_opens_correct_url(self, ledger_with_trade: TradeLedger) -> None:
        app = _LedgerHarness(ledger_with_trade)
        with patch("webbrowser.open") as mock_open:
            async with app.run_test() as pilot:
                await pilot.pause()
                widget = app.widget
                # Synthesize a RowSelected as if the operator clicked
                # the first row. cursor_row indexes _row_sigs.
                from textual.widgets import DataTable

                event = DataTable.RowSelected(
                    data_table=widget,
                    cursor_row=0,
                    row_key=widget.coordinate_to_cell_key((0, 0)).row_key,
                )
                widget.on_data_table_row_selected(event)
                await pilot.pause()
        assert mock_open.called
        opened_url = mock_open.call_args.args[0]
        assert opened_url == f"{ORB_EXPLORER_TX_BASE}{SAMPLE_SIG}"

    async def test_row_out_of_range_is_noop(self, ledger_with_trade: TradeLedger) -> None:
        # Defensive: if a stale RowSelected event arrives after rows
        # were cleared, the handler must not crash.
        app = _LedgerHarness(ledger_with_trade)
        with patch("webbrowser.open") as mock_open:
            async with app.run_test() as pilot:
                await pilot.pause()
                widget = app.widget
                from textual.widgets import DataTable

                event = DataTable.RowSelected(
                    data_table=widget,
                    cursor_row=999,
                    row_key=widget.coordinate_to_cell_key((0, 0)).row_key,
                )
                widget.on_data_table_row_selected(event)
                await pilot.pause()
        assert not mock_open.called

    async def test_row_with_empty_signature_is_noop(self, tmp_path: Path) -> None:
        # A synthetic row (e.g. reconciler placeholder) carries an
        # empty signature; clicking it must NOT open a tx URL with no
        # signature (would 404 on the explorer).
        ledger = TradeLedger(storage_dir=str(tmp_path))
        ledger.append(
            TradeEntry(
                timestamp=now_iso(),
                side="buy",
                input_mint="SOL",
                input_symbol="SOL",
                input_decimals=9,
                input_amount_raw=0,
                input_amount_ui=0.0,
                input_value_usd=0.0,
                output_mint="EN2nnxrg",
                output_decimals=6,
                expected_out_raw=0,
                expected_out_ui=0.0,
                actual_out_raw=0,
                actual_out_ui=0.0,
                output_price_usd=0.0,
                output_value_usd=0.0,
                signature="",  # synthetic / placeholder row
            )
        )
        app = _LedgerHarness(ledger)
        with patch("webbrowser.open") as mock_open:
            async with app.run_test() as pilot:
                await pilot.pause()
                widget = app.widget
                from textual.widgets import DataTable

                event = DataTable.RowSelected(
                    data_table=widget,
                    cursor_row=0,
                    row_key=widget.coordinate_to_cell_key((0, 0)).row_key,
                )
                widget.on_data_table_row_selected(event)
                await pilot.pause()
        assert not mock_open.called


class TestAddTradeKeepsLookupAligned:
    async def test_add_trade_prepends_signature(
        self, ledger_with_trade: TradeLedger, trade: TradeEntry
    ) -> None:
        app = _LedgerHarness(ledger_with_trade)
        async with app.run_test() as pilot:
            await pilot.pause()
            widget = app.widget
            # Append a second trade with a different sig; widget should
            # treat it as the new top row and prepend the sig.
            new_sig = "y" * 80
            new_trade = TradeEntry(
                timestamp=now_iso(),
                side="sell",
                input_mint="EN2nnxrg",
                input_symbol="TARGET",
                input_decimals=6,
                input_amount_raw=50_000_000,
                input_amount_ui=50.0,
                input_value_usd=15.0,
                output_mint="USDC",
                output_decimals=6,
                expected_out_raw=15_000_000,
                expected_out_ui=15.0,
                actual_out_raw=14_950_000,
                actual_out_ui=14.95,
                output_price_usd=1.0,
                output_value_usd=14.95,
                signature=new_sig,
            )
            ledger_with_trade.append(new_trade)
            widget.add_trade(new_trade)
            await pilot.pause()
            # Newest first — new sig at index 0, original at index 1.
            assert widget._row_sigs == [new_sig, SAMPLE_SIG]
