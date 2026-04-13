"""Tests for pod_the_trader.tools.trading_tools."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pod_the_trader.config import Config
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.tools.trading_tools import register_tools
from pod_the_trader.trading.dex import FeasibilityResult, JupiterDex, SwapQuote, TradeExecution
from pod_the_trader.trading.portfolio import Portfolio

SOL_MINT = "So11111111111111111111111111111111111111112"
TEST_MINT = "TokenMintAddress111111111111111111111111111"


@pytest.fixture()
def mock_dex() -> JupiterDex:
    dex = MagicMock(spec=JupiterDex)
    dex.get_quote = AsyncMock(
        return_value=SwapQuote(
            input_mint=SOL_MINT,
            output_mint=TEST_MINT,
            in_amount=100_000_000,
            out_amount=50_000,
            price_impact_pct=0.5,
            slippage_bps=50,
            raw={},
        )
    )
    dex.execute_swap = AsyncMock(
        return_value=TradeExecution(
            success=True,
            signature="swap_sig_123",
            input_mint=SOL_MINT,
            output_mint=TEST_MINT,
            in_amount=100_000_000,
            out_amount=50_000,
        )
    )
    dex.get_token_price = AsyncMock(return_value=0.15)
    dex.check_feasibility = AsyncMock(
        return_value=FeasibilityResult(feasible=True, price_impact_pct=0.5, reason="OK")
    )
    return dex


@pytest.fixture()
def mock_portfolio(tmp_path: Path) -> Portfolio:
    p = MagicMock(spec=Portfolio)
    p.record_trade = MagicMock()
    # Balance checks used by the new amount-resolution logic
    p.get_sol_balance = AsyncMock(return_value=10.0)
    p.get_token_balance = AsyncMock(return_value=1_000_000.0)
    return p


@pytest.fixture()
def registry(
    sample_config: Config, mock_dex: JupiterDex, mock_portfolio: Portfolio
) -> ToolRegistry:
    reg = ToolRegistry()
    register_tools(
        reg,
        config=sample_config,
        jupiter_dex=mock_dex,
        portfolio=mock_portfolio,
        wallet_address="11111111111111111111111111111111",
    )
    return reg


class TestGetSwapQuote:
    async def test_returns_quote(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": TEST_MINT,
                    "amount_in": 0.1,
                },
            )
        )
        assert result["in_amount_raw"] == 100_000_000
        assert result["out_amount_raw"] == 50_000
        assert "summary" in result


class TestExecuteSwap:
    async def test_returns_error_without_keypair(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "execute_swap",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": TEST_MINT,
                    "amount_in": 0.1,
                },
            )
        )
        assert "error" in result
        assert "keypair" in result["error"].lower()


class TestCheckFeasibility:
    async def test_returns_feasibility(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "check_swap_feasibility",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": TEST_MINT,
                    "amount_in": 0.1,
                },
            )
        )
        assert result["feasible"] is True
        assert result["price_impact_pct"] == 0.5


class TestGetTokenPrice:
    async def test_returns_price(self, registry: ToolRegistry) -> None:
        result = json.loads(await registry.execute("get_token_price", {"mint_address": TEST_MINT}))
        assert result["price_usd"] == 0.15
