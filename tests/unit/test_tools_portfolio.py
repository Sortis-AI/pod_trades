"""Tests for pod_the_trader.tools.portfolio_tools."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pod_the_trader.config import Config
from pod_the_trader.tools.portfolio_tools import register_tools
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.trading.portfolio import (
    PnLSummary,
    Portfolio,
    PortfolioSummary,
    TradeRecord,
)

TEST_MINT = "So11111111111111111111111111111111111111112"


@pytest.fixture()
def mock_portfolio() -> Portfolio:
    p = MagicMock(spec=Portfolio)
    p.get_portfolio_value = AsyncMock(
        return_value=PortfolioSummary(
            sol_balance=5.0,
            sol_value_usd=750.0,
            token_balances={TEST_MINT: 100.0},
            token_values_usd={TEST_MINT: 15.0},
            total_value_usd=765.0,
        )
    )
    p.get_token_balance = AsyncMock(return_value=100.0)
    p.get_trade_history = MagicMock(
        return_value=[
            TradeRecord("2026-04-01", "buy", TEST_MINT, TEST_MINT, 1.0, 100.0, 0.15, 15.0, "sig1"),
        ]
    )
    p.calculate_pnl = MagicMock(
        return_value=PnLSummary(
            total_pnl_usd=3.0,
            win_rate=100.0,
            total_trades=2,
            avg_trade_size=16.5,
            largest_win=3.0,
            largest_loss=0.0,
        )
    )
    return p


@pytest.fixture()
def registry(sample_config: Config, mock_portfolio: Portfolio) -> ToolRegistry:
    reg = ToolRegistry()
    register_tools(
        reg,
        portfolio=mock_portfolio,
        wallet_address="11111111111111111111111111111111",
        config=sample_config,
    )
    return reg


class TestGetPortfolioOverview:
    async def test_returns_overview(self, registry: ToolRegistry) -> None:
        result = json.loads(await registry.execute("get_portfolio_overview", {}))
        assert result["sol_balance"] == 5.0
        assert result["total_value_usd"] == 765.0


class TestGetTokenBalance:
    async def test_returns_balance(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute("get_token_balance", {"mint_address": TEST_MINT})
        )
        assert result["balance"] == 100.0


class TestGetTradeHistory:
    async def test_returns_trades(self, registry: ToolRegistry) -> None:
        result = json.loads(await registry.execute("get_trade_history", {"limit": 10}))
        assert result["count"] == 1
        assert result["trades"][0]["side"] == "buy"


class TestCalculatePnl:
    async def test_returns_pnl(self, registry: ToolRegistry) -> None:
        result = json.loads(await registry.execute("calculate_pnl", {}))
        assert result["total_pnl_usd"] == 3.0
        assert result["win_rate"] == 100.0
        assert result["total_trades"] == 2
