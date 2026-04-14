"""Unit tests for the lot ledger reconciler."""

from pathlib import Path

import pytest

from pod_the_trader.data.lot_ledger import (
    KIND_CLOSE,
    KIND_OPEN,
    SOURCE_RECONCILE,
    SOURCE_TRADE,
    LotEvent,
    LotLedger,
    now_iso,
)
from pod_the_trader.data.reconciler import (
    reconcile_mint,
    reconcile_portfolio,
)

SOL_MINT = "So11111111111111111111111111111111111111112"
FAKE_TOKEN = "11111111111111111111111111111111"


@pytest.fixture()
def ledger(tmp_path: Path) -> LotLedger:
    return LotLedger(storage_dir=str(tmp_path))


def _seed_open(ledger: LotLedger, mint: str, qty: float, price: float) -> None:
    ledger.append(
        LotEvent(
            timestamp=now_iso(),
            mint=mint,
            kind=KIND_OPEN,
            qty=qty,
            unit_price_usd=price,
            source=SOURCE_TRADE,
        )
    )


class TestReconcileMint:
    def test_noop_when_balance_matches(self, ledger: LotLedger) -> None:
        _seed_open(ledger, FAKE_TOKEN, 100, 0.10)
        ev = reconcile_mint(
            ledger,
            mint=FAKE_TOKEN,
            actual_qty=100,
            current_price_usd=0.20,
        )
        assert ev is None
        assert ledger.position_state(FAKE_TOKEN).open_qty == pytest.approx(100)

    def test_ignores_sub_dust_delta(self, ledger: LotLedger) -> None:
        _seed_open(ledger, FAKE_TOKEN, 100, 0.10)
        ev = reconcile_mint(
            ledger,
            mint=FAKE_TOKEN,
            actual_qty=100.0000001,
            current_price_usd=0.20,
            dust=1e-3,
        )
        assert ev is None

    def test_positive_delta_creates_open_lot_at_spot_price(self, ledger: LotLedger) -> None:
        _seed_open(ledger, FAKE_TOKEN, 100, 0.10)
        ev = reconcile_mint(
            ledger,
            mint=FAKE_TOKEN,
            actual_qty=250,
            current_price_usd=0.20,
        )
        assert ev is not None
        assert ev.kind == KIND_OPEN
        assert ev.qty == pytest.approx(150)
        assert ev.unit_price_usd == pytest.approx(0.20)
        assert ev.source == SOURCE_RECONCILE

        state = ledger.position_state(FAKE_TOKEN)
        assert state.open_qty == pytest.approx(250)
        # Trading realized P&L unchanged
        assert state.realized_pnl(sources=(SOURCE_TRADE,)) == 0

    def test_negative_delta_creates_close_consuming_lots_fifo(self, ledger: LotLedger) -> None:
        _seed_open(ledger, FAKE_TOKEN, 100, 0.10)
        _seed_open(ledger, FAKE_TOKEN, 100, 0.20)
        ev = reconcile_mint(
            ledger,
            mint=FAKE_TOKEN,
            actual_qty=150,
            current_price_usd=0.30,
        )
        assert ev is not None
        assert ev.kind == KIND_CLOSE
        assert ev.qty == pytest.approx(50)
        assert ev.source == SOURCE_RECONCILE

        state = ledger.position_state(FAKE_TOKEN)
        # 50 units consumed FIFO from the first lot at $0.10
        assert state.open_qty == pytest.approx(150)
        # No trading P&L change
        assert state.realized_pnl(sources=(SOURCE_TRADE,)) == 0

    def test_negative_delta_with_no_basis_is_skipped(self, ledger: LotLedger) -> None:
        # Ledger empty, on-chain says actual < 0? we just skip safely
        ev = reconcile_mint(
            ledger,
            mint=FAKE_TOKEN,
            actual_qty=0,
            current_price_usd=0.20,
        )
        assert ev is None

    def test_deposit_appears_in_open_lots_with_spot_basis(self, ledger: LotLedger) -> None:
        ev = reconcile_mint(
            ledger,
            mint=FAKE_TOKEN,
            actual_qty=500,
            current_price_usd=0.20,
        )
        assert ev is not None
        state = ledger.position_state(FAKE_TOKEN)
        assert state.open_qty == pytest.approx(500)
        assert state.cost_basis_usd == pytest.approx(100)
        assert state.avg_cost_basis == pytest.approx(0.20)


class TestExternalSwapScenario:
    def test_external_swap_decomposes_into_token_out_sol_in(self, ledger: LotLedger) -> None:
        # Seed: the bot thinks we have 1 SOL and 1000 tokens
        _seed_open(ledger, SOL_MINT, 1.0, 80.0)
        _seed_open(ledger, FAKE_TOKEN, 1000, 0.10)

        # User performed an external swap: sold 500 tokens, got 0.6 SOL
        # Bot now sees 1.6 SOL and 500 tokens on-chain.
        emitted = reconcile_portfolio(
            ledger,
            sol_mint=SOL_MINT,
            sol_balance=1.6,
            sol_price_usd=80.0,
            token_mint=FAKE_TOKEN,
            token_balance=500,
            token_price_usd=0.12,
        )
        assert len(emitted) == 2
        kinds = {(e.mint, e.kind) for e in emitted}
        assert (SOL_MINT, KIND_OPEN) in kinds
        assert (FAKE_TOKEN, KIND_CLOSE) in kinds

        sol_state = ledger.position_state(SOL_MINT)
        token_state = ledger.position_state(FAKE_TOKEN)
        assert sol_state.open_qty == pytest.approx(1.6)
        assert token_state.open_qty == pytest.approx(500)
        # No trading P&L should have been booked on either side
        assert sol_state.realized_pnl(sources=(SOURCE_TRADE,)) == 0
        assert token_state.realized_pnl(sources=(SOURCE_TRADE,)) == 0


class TestReconcilePortfolio:
    def test_handles_sol_delta_only_when_no_token(self, ledger: LotLedger) -> None:
        _seed_open(ledger, SOL_MINT, 1.0, 80.0)
        emitted = reconcile_portfolio(
            ledger,
            sol_mint=SOL_MINT,
            sol_balance=2.0,
            sol_price_usd=85.0,
            token_mint="",
            token_balance=0,
            token_price_usd=0,
        )
        assert len(emitted) == 1
        assert emitted[0].mint == SOL_MINT
        assert emitted[0].kind == KIND_OPEN
        assert emitted[0].qty == pytest.approx(1.0)

    def test_respects_dust_thresholds(self, ledger: LotLedger) -> None:
        _seed_open(ledger, SOL_MINT, 1.0, 80.0)
        _seed_open(ledger, FAKE_TOKEN, 1000, 0.10)
        emitted = reconcile_portfolio(
            ledger,
            sol_mint=SOL_MINT,
            sol_balance=1.0000001,  # sub-dust for SOL
            sol_price_usd=80.0,
            token_mint=FAKE_TOKEN,
            token_balance=1000.0005,  # sub-dust for token (default 1e-3)
            token_price_usd=0.10,
        )
        assert emitted == []
