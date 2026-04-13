"""Tests for pod_the_trader.tools.market_tools."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pod_the_trader.config import Config
from pod_the_trader.tools.market_tools import register_tools
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.trading.dex import JupiterDex

SOL_MINT = "So11111111111111111111111111111111111111112"


@pytest.fixture()
def mock_dex() -> JupiterDex:
    dex = MagicMock(spec=JupiterDex)
    dex.get_token_price = AsyncMock(return_value=150.0)
    return dex


@pytest.fixture()
def registry(sample_config: Config, mock_dex: JupiterDex) -> ToolRegistry:
    reg = ToolRegistry()
    register_tools(reg, config=sample_config, jupiter_dex=mock_dex)
    return reg


class TestGetMarketPrice:
    async def test_returns_price(self, registry: ToolRegistry) -> None:
        result = json.loads(await registry.execute("get_market_price", {"mint_address": SOL_MINT}))
        assert result["price_usd"] == 150.0
        assert result["mint"] == SOL_MINT


class TestAnalyzeMarketConditions:
    async def test_returns_structured_analysis(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute("analyze_market_conditions", {"mint_address": SOL_MINT})
        )
        assert "analysis" in result
        assert result["price_usd"] == 150.0
        assert isinstance(result["analysis"], dict)


class TestGetTargetTokenStatus:
    async def test_returns_price_when_configured(self, registry: ToolRegistry) -> None:
        result = json.loads(await registry.execute("get_target_token_status", {}))
        assert "price_usd" in result
        assert result["price_usd"] == 150.0

    async def test_returns_error_when_not_configured(self, mock_dex: JupiterDex) -> None:
        import os
        from unittest.mock import patch

        # Build a config with empty target
        reg = ToolRegistry()
        with patch.dict(os.environ, {"TARGET_TOKEN_ADDRESS": ""}):
            # Can't easily construct Config with empty target (validation blocks it)
            # Instead test via a mock config
            mock_config = MagicMock()
            mock_config.get = MagicMock(return_value="")
            register_tools(reg, config=mock_config, jupiter_dex=mock_dex)

        result = json.loads(await reg.execute("get_target_token_status", {}))
        assert "error" in result
