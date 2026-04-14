"""Migration: replay legacy TradeLedger entries into the lot ledger."""

from pathlib import Path

import pytest

from pod_the_trader.data.ledger import TradeEntry, TradeLedger
from pod_the_trader.data.lot_ledger import (
    LotLedger,
    migrate_from_trade_ledger,
    now_iso,
)
from pod_the_trader.data.reconciler import reconcile_portfolio

SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN = "TARGET11111111111111111111111111111111111"


@pytest.fixture()
def storage(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def trade_ledger(storage: Path) -> TradeLedger:
    return TradeLedger(storage_dir=str(storage))


@pytest.fixture()
def lot_ledger(storage: Path) -> LotLedger:
    return LotLedger(storage_dir=str(storage))


def _buy(
    input_sol: float, output_tokens: float, sol_price: float, token_price: float
) -> TradeEntry:
    return TradeEntry(
        timestamp=now_iso(),
        side="buy",
        input_mint=SOL_MINT,
        input_decimals=9,
        input_amount_ui=input_sol,
        input_price_usd=sol_price,
        input_value_usd=input_sol * sol_price,
        output_mint=TOKEN,
        output_decimals=6,
        actual_out_ui=output_tokens,
        output_price_usd=token_price,
        output_value_usd=output_tokens * token_price,
        sol_price_usd=sol_price,
        gas_lamports=5000,
        gas_sol=0.000005,
        signature="sig_buy",
    )


def _sell(
    input_tokens: float, output_sol: float, sol_price: float, token_price: float
) -> TradeEntry:
    return TradeEntry(
        timestamp=now_iso(),
        side="sell",
        input_mint=TOKEN,
        input_decimals=6,
        input_amount_ui=input_tokens,
        input_price_usd=token_price,
        input_value_usd=input_tokens * token_price,
        output_mint=SOL_MINT,
        output_decimals=9,
        actual_out_ui=output_sol,
        output_price_usd=sol_price,
        output_value_usd=output_sol * sol_price,
        sol_price_usd=sol_price,
        gas_lamports=5000,
        gas_sol=0.000005,
        signature="sig_sell",
    )


class TestMigration:
    def test_migration_is_idempotent(
        self, trade_ledger: TradeLedger, lot_ledger: LotLedger
    ) -> None:
        trade_ledger.append(_buy(0.1, 500, 80.0, 0.016))
        # First migration emits events
        n1 = migrate_from_trade_ledger(lot_ledger, trade_ledger.read_all(), sol_mint=SOL_MINT)
        assert n1 > 0
        # Second migration must skip (ledger is non-empty)
        n2 = migrate_from_trade_ledger(lot_ledger, trade_ledger.read_all(), sol_mint=SOL_MINT)
        assert n2 == 0

    def test_buy_then_sell_replays_with_correct_pnl(
        self, trade_ledger: TradeLedger, lot_ledger: LotLedger
    ) -> None:
        # Buy 500 tokens at $0.016 each (paying 0.1 SOL @ $80)
        trade_ledger.append(_buy(0.1, 500, 80.0, 0.016))
        # Sell 200 of them at $0.025 (output 0.0625 SOL @ $80)
        trade_ledger.append(_sell(200, 0.0625, 80.0, 0.025))

        migrate_from_trade_ledger(lot_ledger, trade_ledger.read_all(), sol_mint=SOL_MINT)

        token_state = lot_ledger.position_state(TOKEN)
        assert token_state.open_qty == pytest.approx(300)
        # Realized: 200 * (0.025 - 0.016) = 1.8
        assert token_state.realized_pnl() == pytest.approx(1.8)

    def test_sol_legs_skipped_during_migration(
        self, trade_ledger: TradeLedger, lot_ledger: LotLedger
    ) -> None:
        # Migration only replays the token leg of each swap. SOL state is
        # untouched so the reconciler can absorb actual on-chain SOL fresh.
        trade_ledger.append(_buy(0.1, 500, 80.0, 0.016))
        trade_ledger.append(_sell(200, 0.0625, 80.0, 0.025))
        migrate_from_trade_ledger(lot_ledger, trade_ledger.read_all(), sol_mint=SOL_MINT)

        sol_state = lot_ledger.position_state(SOL_MINT)
        assert sol_state.open_qty == 0
        assert sol_state.realized_pnl() == 0

        # Token state is fully populated with the trade history
        token_state = lot_ledger.position_state(TOKEN)
        assert token_state.open_qty == pytest.approx(300)


class TestRealisticMigrationThenReconcile:
    """End-to-end: migrate a messy legacy trade history, then reconcile against
    on-chain truth.

    Mirrors the real-world shape we hit when porting an existing wallet to
    the lot ledger: the legacy TradeLedger only knows about bot-initiated
    swaps, but the wallet had been funded externally before tracking started.
    Migration replays every trade as token-leg events; sells frequently
    exceed prior buys because the bot was selling tokens it never recorded
    buying. The startup reconciler then absorbs the delta against actual
    on-chain balance and the resulting state should match reality.
    """

    def test_sells_exceeding_buys_then_reconcile_to_real_balance(
        self, trade_ledger: TradeLedger, lot_ledger: LotLedger
    ) -> None:
        # Phase 1: a realistic mixed history. The bot bought ~1500 tokens
        # worth of position over time AND sold ~5000 tokens (the extra 3500
        # came from an external deposit that pre-dated tracking).
        trade_ledger.append(_buy(0.05, 500, 80.0, 0.0001))  # 500 @ 0.0001
        trade_ledger.append(_buy(0.05, 1000, 80.0, 0.00012))  # 1000 @ 0.00012
        trade_ledger.append(_sell(2000, 0.32, 80.0, 0.000128))  # sells 2000 @ 0.000128
        trade_ledger.append(_buy(0.10, 800, 80.0, 0.000125))  # 800 @ 0.000125
        trade_ledger.append(_sell(3000, 0.45, 80.0, 0.00012))  # sells 3000 @ 0.00012

        n = migrate_from_trade_ledger(lot_ledger, trade_ledger.read_all(), sol_mint=SOL_MINT)
        # 5 token-leg events: 3 opens + 2 closes
        assert n == 5

        # Phase 2: pre-reconciliation snapshot. The migration left some
        # phantom state because the sells consumed the entire migrated open
        # basis (and then some — the unmatched portion is silently dropped).
        # The point is that the open_qty here does NOT reflect on-chain
        # truth; the reconciler is what fixes that.
        pre = lot_ledger.position_state(TOKEN)
        # We bought 2300, sold 5000 → 2700 of unmatched closes were dropped.
        # Open lots are empty.
        assert pre.open_qty == pytest.approx(0)
        # Realized P&L is whatever could be matched (2300 tokens worth).
        assert pre.realized_pnl() != 0

        # Phase 3: reconcile against actual on-chain balance. Suppose the
        # wallet currently holds 1200 tokens (the user has deposited some
        # more or never sold them all externally).
        emitted = reconcile_portfolio(
            lot_ledger,
            sol_mint=SOL_MINT,
            sol_balance=2.5,
            sol_price_usd=80.0,
            token_mint=TOKEN,
            token_balance=1200.0,
            token_price_usd=0.00015,
        )
        # Two reconcile events: SOL deposit + token deposit
        assert len(emitted) == 2

        # Phase 4: post-reconciliation state must match reality
        post = lot_ledger.position_state(TOKEN)
        assert post.open_qty == pytest.approx(1200)
        # Cost basis = qty × spot price at reconciliation time
        assert post.cost_basis_usd == pytest.approx(1200 * 0.00015)
        assert post.avg_cost_basis == pytest.approx(0.00015)
        # Trading realized P&L is unchanged by reconciliation
        assert post.realized_pnl() == pytest.approx(pre.realized_pnl())

        # SOL state was empty before reconciliation (migration skips SOL).
        # Now it should reflect the actual on-chain balance.
        sol_post = lot_ledger.position_state(SOL_MINT)
        assert sol_post.open_qty == pytest.approx(2.5)
        assert sol_post.cost_basis_usd == pytest.approx(2.5 * 80.0)
        # No trading P&L on SOL — only reconcile events touched it
        assert sol_post.realized_pnl() == 0

    def test_summary_after_migration_and_reconcile_is_internally_consistent(
        self, trade_ledger: TradeLedger, lot_ledger: LotLedger
    ) -> None:
        # Migrate a small history then reconcile, and verify the summary
        # dict's fields all agree with each other.
        trade_ledger.append(_buy(0.10, 1000, 80.0, 0.0001))
        trade_ledger.append(_sell(400, 0.06, 80.0, 0.00012))
        migrate_from_trade_ledger(lot_ledger, trade_ledger.read_all(), sol_mint=SOL_MINT)

        # Reconcile to a slightly different on-chain balance (some external
        # deposit happened while the bot was offline).
        reconcile_portfolio(
            lot_ledger,
            sol_mint=SOL_MINT,
            sol_balance=1.0,
            sol_price_usd=80.0,
            token_mint=TOKEN,
            token_balance=800.0,  # 600 from migration + 200 deposited
            token_price_usd=0.00013,
        )

        s = lot_ledger.summary(TOKEN, current_price_usd=0.00013)

        # Open qty matches reality
        assert s["open_qty"] == pytest.approx(800.0)
        # position_value = open_qty × current_price
        assert s["position_value_usd"] == pytest.approx(800 * 0.00013)
        # total = realized + unrealized
        assert s["total_pnl_usd"] == pytest.approx(s["realized_pnl_usd"] + s["unrealized_pnl_usd"])
        # Realized came from one trade close
        assert s["trade_close_count"] == 1
        assert s["realized_pnl_usd"] == pytest.approx(400 * (0.00012 - 0.0001))
        # cost_basis_usd = open_qty × avg_cost_basis (within fp tolerance)
        assert s["cost_basis_usd"] == pytest.approx(s["open_qty"] * s["avg_cost_basis"])
