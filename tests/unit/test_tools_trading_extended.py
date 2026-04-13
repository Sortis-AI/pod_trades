"""Extended tests for pod_the_trader.tools.trading_tools — execute_swap path."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from solders.keypair import Keypair

from pod_the_trader.config import Config
from pod_the_trader.data.ledger import TradeLedger
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.tools.trading_tools import register_tools
from pod_the_trader.trading.dex import JupiterDex, TradeExecution
from pod_the_trader.trading.portfolio import Portfolio

SOL_MINT = "So11111111111111111111111111111111111111112"
TEST_MINT = "TokenMintAddress111111111111111111111111111"


@pytest.fixture()
def mock_dex() -> JupiterDex:
    dex = MagicMock(spec=JupiterDex)
    dex.get_token_price = AsyncMock(return_value=0.15)
    dex.execute_swap = AsyncMock(
        return_value=TradeExecution(
            success=True,
            signature="swap_sig",
            input_mint=SOL_MINT,
            output_mint=TEST_MINT,
            in_amount=100_000_000,
            out_amount=50_000_000,
            actual_out_amount=49_500_000,
            gas_lamports=5000,
            block_slot=12345,
            block_time=1700000000,
            price_impact_pct=0.3,
            slippage_bps_requested=50,
        )
    )
    return dex


@pytest.fixture()
def mock_portfolio() -> Portfolio:
    p = MagicMock(spec=Portfolio)
    p.record_trade = MagicMock()
    p.get_sol_balance = AsyncMock(return_value=10.0)
    p.get_token_balance = AsyncMock(return_value=1_000_000.0)
    return p


@pytest.fixture()
def ledger(tmp_path: Path) -> TradeLedger:
    return TradeLedger(storage_dir=str(tmp_path))


@pytest.fixture()
def registry_with_keypair(
    sample_config: Config,
    mock_dex: JupiterDex,
    mock_portfolio: Portfolio,
    ledger: TradeLedger,
) -> ToolRegistry:
    reg = ToolRegistry()
    register_tools(
        reg,
        config=sample_config,
        jupiter_dex=mock_dex,
        portfolio=mock_portfolio,
        wallet_address="11111111111111111111111111111111",
        ledger=ledger,
        session_id="testsession",
    )
    kp = Keypair()
    reg._set_trading_keypair(kp)
    return reg


class TestExecuteSwapWithKeypair:
    async def test_successful_swap_records_to_ledger(
        self,
        registry_with_keypair: ToolRegistry,
        ledger: TradeLedger,
    ) -> None:
        result = json.loads(
            await registry_with_keypair.execute(
                "execute_swap",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": TEST_MINT,
                    "amount_in": 0.1,
                },
            )
        )
        assert result["success"] is True
        assert result["signature"] == "swap_sig"

        trades = ledger.read_all()
        assert len(trades) == 1
        t = trades[0]
        assert t.side == "buy"
        assert t.input_mint == SOL_MINT
        assert t.output_mint == TEST_MINT
        assert t.session_id == "testsession"
        assert t.gas_lamports == 5000
        assert t.actual_out_raw == 49_500_000
        assert t.expected_out_raw == 50_000_000
        assert t.slippage_bps_realized > 0  # actual was less than expected
        assert t.signature == "swap_sig"
        assert t.block_slot == 12345

    async def test_failed_swap_no_ledger_entry(
        self,
        registry_with_keypair: ToolRegistry,
        mock_dex: JupiterDex,
        ledger: TradeLedger,
    ) -> None:
        mock_dex.execute_swap = AsyncMock(
            return_value=TradeExecution(
                success=False,
                input_mint=SOL_MINT,
                output_mint=TEST_MINT,
                error="Insufficient funds",
            )
        )
        result = json.loads(
            await registry_with_keypair.execute(
                "execute_swap",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": TEST_MINT,
                    "amount_in": 0.1,
                },
            )
        )
        assert result["success"] is False
        assert len(ledger.read_all()) == 0
