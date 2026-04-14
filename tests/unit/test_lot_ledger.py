"""Unit tests for the lot-based cost-basis ledger."""

from pathlib import Path

import pytest

from pod_the_trader.data.lot_ledger import (
    KIND_CLOSE,
    KIND_OPEN,
    SOURCE_GAS,
    SOURCE_RECONCILE,
    SOURCE_TRADE,
    LotEvent,
    LotLedger,
    emit_trade_events,
    now_iso,
)

SOL_MINT = "So11111111111111111111111111111111111111112"
FAKE_TOKEN = "11111111111111111111111111111111"


@pytest.fixture()
def ledger(tmp_path: Path) -> LotLedger:
    return LotLedger(storage_dir=str(tmp_path))


def _open(mint: str, qty: float, price: float, source: str = SOURCE_TRADE) -> LotEvent:
    return LotEvent(
        timestamp=now_iso(),
        mint=mint,
        kind=KIND_OPEN,
        qty=qty,
        unit_price_usd=price,
        source=source,
    )


def _close(mint: str, qty: float, price: float, source: str = SOURCE_TRADE) -> LotEvent:
    return LotEvent(
        timestamp=now_iso(),
        mint=mint,
        kind=KIND_CLOSE,
        qty=qty,
        unit_price_usd=price,
        source=source,
    )


class TestAppendAndRead:
    def test_empty_ledger_returns_empty_state(self, ledger: LotLedger) -> None:
        assert ledger.read_all() == []
        state = ledger.position_state(FAKE_TOKEN)
        assert state.open_qty == 0
        assert state.realized_pnl() == 0

    def test_append_single_open_lot(self, ledger: LotLedger) -> None:
        ledger.append(_open(FAKE_TOKEN, 100, 0.5))
        state = ledger.position_state(FAKE_TOKEN)
        assert state.open_qty == 100
        assert state.cost_basis_usd == 50
        assert state.avg_cost_basis == 0.5

    def test_rejects_zero_qty(self, ledger: LotLedger) -> None:
        with pytest.raises(ValueError):
            ledger.append(_open(FAKE_TOKEN, 0, 1.0))

    def test_rejects_invalid_source(self, ledger: LotLedger) -> None:
        with pytest.raises(ValueError):
            ledger.append(
                LotEvent(
                    timestamp=now_iso(),
                    mint=FAKE_TOKEN,
                    kind=KIND_OPEN,
                    qty=1,
                    unit_price_usd=1,
                    source="bogus",
                )
            )


class TestFIFOConsumption:
    def test_close_entirely_consumes_first_lot(self, ledger: LotLedger) -> None:
        ledger.append(_open(FAKE_TOKEN, 100, 0.10))
        ledger.append(_open(FAKE_TOKEN, 100, 0.20))
        ledger.append(_close(FAKE_TOKEN, 100, 0.15))

        state = ledger.position_state(FAKE_TOKEN)
        assert state.open_qty == 100
        # The second lot remains, unchanged
        assert state.avg_cost_basis == 0.20
        # Realized: sold 100 from the $0.10 lot at $0.15 → +$5
        assert state.realized_pnl() == pytest.approx(5.0)

    def test_close_spans_multiple_lots(self, ledger: LotLedger) -> None:
        ledger.append(_open(FAKE_TOKEN, 100, 0.10))
        ledger.append(_open(FAKE_TOKEN, 100, 0.20))
        ledger.append(_close(FAKE_TOKEN, 150, 0.25))

        state = ledger.position_state(FAKE_TOKEN)
        assert state.open_qty == pytest.approx(50)
        # Remaining 50 units all came from the $0.20 lot
        assert state.avg_cost_basis == pytest.approx(0.20)
        # Realized: 100*(0.25-0.10) + 50*(0.25-0.20) = 15 + 2.5 = 17.5
        assert state.realized_pnl() == pytest.approx(17.5)

    def test_partial_close_splits_head_lot(self, ledger: LotLedger) -> None:
        ledger.append(_open(FAKE_TOKEN, 100, 0.10))
        ledger.append(_close(FAKE_TOKEN, 40, 0.30))

        state = ledger.position_state(FAKE_TOKEN)
        assert state.open_qty == pytest.approx(60)
        assert state.cost_basis_usd == pytest.approx(6.0)
        # 40 * (0.30 - 0.10) = 8
        assert state.realized_pnl() == pytest.approx(8.0)


class TestSourceFiltering:
    def test_reconcile_close_does_not_count_toward_trading_pnl(self, ledger: LotLedger) -> None:
        ledger.append(_open(FAKE_TOKEN, 100, 0.10, source=SOURCE_TRADE))
        # External withdrawal: 40 tokens left, "priced" at $1.00 — shouldn't
        # produce any realized trading P&L
        ledger.append(_close(FAKE_TOKEN, 40, 1.00, source=SOURCE_RECONCILE))

        state = ledger.position_state(FAKE_TOKEN)
        assert state.open_qty == pytest.approx(60)
        # No trading P&L booked
        assert state.realized_pnl(sources=(SOURCE_TRADE,)) == 0
        # But reconcile P&L does exist if we ask for it
        assert state.realized_pnl(sources=(SOURCE_RECONCILE,)) == pytest.approx(36.0)

    def test_gas_close_tracked_separately(self, ledger: LotLedger) -> None:
        ledger.append(_open(SOL_MINT, 1.0, 80.0))  # 1 SOL @ $80
        ledger.append(_close(SOL_MINT, 0.001, 80.0, source=SOURCE_GAS))

        state = ledger.position_state(SOL_MINT)
        assert state.open_qty == pytest.approx(0.999)
        assert state.realized_pnl() == 0  # gas isn't trade-sourced
        assert state.gas_usd() == pytest.approx(0.08)


class TestUnrealizedAndTotal:
    def test_unrealized_updates_with_current_price(self, ledger: LotLedger) -> None:
        ledger.append(_open(FAKE_TOKEN, 100, 0.10))
        state = ledger.position_state(FAKE_TOKEN)
        assert state.unrealized_pnl(0.10) == 0
        assert state.unrealized_pnl(0.15) == pytest.approx(5.0)
        assert state.unrealized_pnl(0.05) == pytest.approx(-5.0)

    def test_total_pnl_sums_realized_and_unrealized(self, ledger: LotLedger) -> None:
        ledger.append(_open(FAKE_TOKEN, 100, 0.10))
        ledger.append(_open(FAKE_TOKEN, 100, 0.20))
        ledger.append(_close(FAKE_TOKEN, 50, 0.30))

        state = ledger.position_state(FAKE_TOKEN)
        # Realized on 50 units sold at 0.30 from 0.10 lot: 50*(0.30-0.10)=10
        assert state.realized_pnl() == pytest.approx(10)
        # Remaining 150 units at avg (50*0.10+100*0.20)/150 ≈ 0.1667
        assert state.open_qty == pytest.approx(150)
        # At current $0.25: unrealized = 150*0.25 - (50*0.10 + 100*0.20) = 37.5 - 25 = 12.5
        assert state.unrealized_pnl(0.25) == pytest.approx(12.5)
        assert state.total_pnl(0.25) == pytest.approx(22.5)


class TestPersistence:
    def test_round_trips_through_disk(self, tmp_path: Path) -> None:
        a = LotLedger(storage_dir=str(tmp_path))
        a.append(_open(FAKE_TOKEN, 100, 0.10))
        a.append(_close(FAKE_TOKEN, 30, 0.20))

        b = LotLedger(storage_dir=str(tmp_path))
        state = b.position_state(FAKE_TOKEN)
        assert state.open_qty == pytest.approx(70)
        assert state.realized_pnl() == pytest.approx(3.0)


class TestEmitTradeEvents:
    def test_buy_produces_input_close_output_open_gas(self) -> None:
        events = emit_trade_events(
            timestamp="2026-04-14T12:00:00+00:00",
            input_mint=SOL_MINT,
            input_qty=0.1,
            input_price_usd=80.0,
            output_mint=FAKE_TOKEN,
            output_qty=500.0,
            output_price_usd=0.016,
            gas_sol=0.0005,
            sol_price_usd=80.0,
            sol_mint=SOL_MINT,
            tx_sig="sig123",
        )
        assert len(events) == 3
        kinds = [(e.mint[:8], e.kind, e.source) for e in events]
        assert (SOL_MINT[:8], KIND_CLOSE, SOURCE_TRADE) in kinds
        assert (FAKE_TOKEN[:8], KIND_OPEN, SOURCE_TRADE) in kinds
        assert (SOL_MINT[:8], KIND_CLOSE, SOURCE_GAS) in kinds

    def test_zero_gas_omits_gas_event(self) -> None:
        events = emit_trade_events(
            timestamp=now_iso(),
            input_mint=FAKE_TOKEN,
            input_qty=500.0,
            input_price_usd=0.016,
            output_mint=SOL_MINT,
            output_qty=0.1,
            output_price_usd=80.0,
            gas_sol=0.0,
            sol_price_usd=80.0,
            sol_mint=SOL_MINT,
        )
        assert len(events) == 2
        assert all(e.source == SOURCE_TRADE for e in events)


class TestSummary:
    def test_summary_returns_display_ready_fields(self, ledger: LotLedger) -> None:
        ledger.append(_open(FAKE_TOKEN, 100, 0.10))
        ledger.append(_close(FAKE_TOKEN, 40, 0.15))

        s = ledger.summary(FAKE_TOKEN, current_price_usd=0.20)
        assert s["open_qty"] == pytest.approx(60)
        assert s["cost_basis_usd"] == pytest.approx(6.0)
        assert s["avg_cost_basis"] == pytest.approx(0.10)
        assert s["position_value_usd"] == pytest.approx(12.0)
        # realized: 40*(0.15-0.10) = 2
        assert s["realized_pnl_usd"] == pytest.approx(2.0)
        # unrealized: 60*0.20 - 6 = 6
        assert s["unrealized_pnl_usd"] == pytest.approx(6.0)
        assert s["total_pnl_usd"] == pytest.approx(8.0)
        assert s["trade_close_count"] == 1

    def test_summary_unknown_mint_is_zeroed(self, ledger: LotLedger) -> None:
        s = ledger.summary("unknown_mint", current_price_usd=1.0)
        assert s["open_qty"] == 0
        assert s["realized_pnl_usd"] == 0
        assert s["total_pnl_usd"] == 0
