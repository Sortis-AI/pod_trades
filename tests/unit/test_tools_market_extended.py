"""Extended tests for pod_the_trader.tools.market_tools — get_token_details, error paths."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

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


class TestGetTokenDetails:
    @respx.mock
    async def test_returns_merged_details(self, registry: ToolRegistry) -> None:
        respx.get("https://lite-api.jup.ag/tokens/v2/search").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": SOL_MINT,
                        "symbol": "SOL",
                        "name": "Wrapped SOL",
                        "decimals": 9,
                    }
                ],
            )
        )
        result = json.loads(await registry.execute("get_token_details", {"mint_address": SOL_MINT}))
        assert result["symbol"] == "SOL"
        assert result["price_usd"] == 150.0

    @respx.mock
    async def test_token_not_in_list(self, registry: ToolRegistry) -> None:
        respx.get("https://lite-api.jup.ag/tokens/v2/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = json.loads(
            await registry.execute("get_token_details", {"mint_address": "unknown"})
        )
        assert result["metadata"] == "not found"
        assert result["price_usd"] == 150.0


class TestAnalyzeMarketError:
    async def test_returns_error_on_failure(self, sample_config: Config) -> None:
        dex = MagicMock(spec=JupiterDex)
        dex.get_token_price = AsyncMock(side_effect=Exception("API down"))
        reg = ToolRegistry()
        register_tools(reg, config=sample_config, jupiter_dex=dex)

        result = json.loads(
            await reg.execute("analyze_market_conditions", {"mint_address": SOL_MINT})
        )
        assert "error" in result


class TestTargetTokenError:
    async def test_price_fetch_error(self, sample_config: Config) -> None:
        dex = MagicMock(spec=JupiterDex)
        dex.get_token_price = AsyncMock(side_effect=Exception("no route"))
        reg = ToolRegistry()
        register_tools(reg, config=sample_config, jupiter_dex=dex)

        result = json.loads(await reg.execute("get_target_token_status", {}))
        assert "error" in result
