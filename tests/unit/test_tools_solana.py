"""Tests for pod_the_trader.tools.solana_tools."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.tools.solana_tools import register_tools


@pytest.fixture()
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_tools(reg, rpc_url="https://api.devnet.solana.com")
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
        # Build a fake token account row as returned by getTokenAccountsByOwner
        acc = MagicMock()
        acc.pubkey = "TokenAcct1"
        acc.account.data.parsed = {
            "info": {
                "tokenAmount": {
                    "amount": "500000000",
                    "decimals": 6,
                    "uiAmount": 500.0,
                    "uiAmountString": "500.0",
                }
            }
        }
        resp = MagicMock()
        resp.value = [acc]

        mock_client = AsyncMock()
        mock_client.get_token_accounts_by_owner_json_parsed = AsyncMock(
            return_value=resp
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("pod_the_trader.tools.solana_tools.AsyncClient", return_value=mock_client):
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
