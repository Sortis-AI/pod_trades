"""Extended tests for pod_the_trader.tools.solana_tools — token info, edge cases."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.tools.solana_tools import register_tools

SOL_MINT = "So11111111111111111111111111111111111111112"


@pytest.fixture()
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_tools(reg, rpc_url="https://api.devnet.solana.com")
    return reg


class TestGetTokenInfo:
    @respx.mock
    async def test_found(self, registry: ToolRegistry) -> None:
        respx.get("https://lite-api.jup.ag/tokens/v2/search").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": SOL_MINT,
                        "symbol": "SOL",
                        "name": "Wrapped SOL",
                        "decimals": 9,
                        "icon": "https://logo.png",
                    }
                ],
            )
        )
        result = json.loads(await registry.execute("get_token_info", {"mint_address": SOL_MINT}))
        assert result["symbol"] == "SOL"
        assert result["decimals"] == 9

    @respx.mock
    async def test_not_found(self, registry: ToolRegistry) -> None:
        respx.get("https://lite-api.jup.ag/tokens/v2/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = json.loads(
            await registry.execute("get_token_info", {"mint_address": "unknown_mint"})
        )
        assert "error" in result


class TestGetSplTokenBalanceZero:
    async def test_returns_zero_on_null_value(self, registry: ToolRegistry) -> None:
        mock_resp = MagicMock()
        mock_resp.value = None

        mock_client = AsyncMock()
        mock_client.get_token_account_balance = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "pod_the_trader.tools.solana_tools.AsyncClient",
            return_value=mock_client,
        ):
            result = json.loads(
                await registry.execute(
                    "get_spl_token_balance",
                    {
                        "owner_address": "11111111111111111111111111111111",
                        "mint_address": SOL_MINT,
                    },
                )
            )

        assert result["balance"] == 0.0

    async def test_returns_zero_on_exception(self, registry: ToolRegistry) -> None:
        mock_client = AsyncMock()
        mock_client.get_token_account_balance = AsyncMock(side_effect=Exception("not found"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "pod_the_trader.tools.solana_tools.AsyncClient",
            return_value=mock_client,
        ):
            result = json.loads(
                await registry.execute(
                    "get_spl_token_balance",
                    {
                        "owner_address": "11111111111111111111111111111111",
                        "mint_address": SOL_MINT,
                    },
                )
            )

        assert result["balance"] == 0.0
