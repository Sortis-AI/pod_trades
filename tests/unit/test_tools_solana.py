"""Tests for pod_the_trader.tools.solana_tools."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.tools.solana_tools import register_tools
from pod_the_trader.trading.portfolio import Portfolio


@pytest.fixture()
def portfolio() -> Portfolio:
    return Portfolio(rpc_url="https://api.devnet.solana.com", jupiter_dex=MagicMock())


@pytest.fixture()
def registry(portfolio: Portfolio) -> ToolRegistry:
    reg = ToolRegistry()
    register_tools(reg, rpc_url="https://api.devnet.solana.com", portfolio=portfolio)
    return reg


class TestGetSolanaBalance:
    async def test_returns_balance(self, registry: ToolRegistry) -> None:
        mock_resp = MagicMock()
        mock_resp.value = 3_000_000_000

        mock_client = AsyncMock()
        mock_client.get_balance = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("pod_the_trader.tools.solana_tools.AsyncClient", return_value=mock_client):
            result = json.loads(
                await registry.execute(
                    "get_solana_balance",
                    {"address": "11111111111111111111111111111111"},
                )
            )

        assert result["balance_sol"] == 3.0
        assert result["balance_lamports"] == 3_000_000_000


class TestGetSplTokenBalance:
    async def test_returns_token_balance(self, registry: ToolRegistry) -> None:
        # Primary path is the direct ATA balance read: legacy ATA holds 500,
        # the Token-2022 ATA doesn't exist.
        legacy_bal = MagicMock()
        legacy_bal.value.ui_amount = 500.0
        not_found = Exception(
            'RPCException(InvalidParamsMessage { message: "Invalid param: '
            'could not find account" })'
        )

        mock_client = AsyncMock()
        mock_client.get_token_account_balance = AsyncMock(side_effect=[legacy_bal, not_found])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        # get_spl_token_balance now delegates to Portfolio.get_token_balance
        # so we must patch the AsyncClient that path uses.
        with patch("pod_the_trader.trading.portfolio.AsyncClient", return_value=mock_client):
            result = json.loads(
                await registry.execute(
                    "get_spl_token_balance",
                    {
                        "owner_address": "11111111111111111111111111111111",
                        "mint_address": "So11111111111111111111111111111111111111112",
                    },
                )
            )

        # Dedupe across the two program queries → single account value
        assert result["balance"] == 500.0


class TestGetRecentTransactions:
    async def test_returns_transactions(self, registry: ToolRegistry) -> None:
        mock_sig = MagicMock()
        mock_sig.signature = "fakesig123"
        mock_sig.slot = 100
        mock_sig.block_time = 1700000000
        mock_sig.err = None

        mock_resp = MagicMock()
        mock_resp.value = [mock_sig]

        mock_client = AsyncMock()
        mock_client.get_signatures_for_address = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("pod_the_trader.tools.solana_tools.AsyncClient", return_value=mock_client):
            result = json.loads(
                await registry.execute(
                    "get_recent_transactions",
                    {"address": "11111111111111111111111111111111", "limit": 5},
                )
            )

        assert result["count"] == 1
        assert result["transactions"][0]["slot"] == 100
