"""Extended tests for pod_the_trader.trading.portfolio — portfolio value."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pod_the_trader.trading.dex import JupiterDex
from pod_the_trader.trading.portfolio import Portfolio

SOL_MINT = "So11111111111111111111111111111111111111112"
TEST_MINT = "TokenMintAddress111111111111111111111111111"


@pytest.fixture()
def mock_dex() -> JupiterDex:
    dex = MagicMock(spec=JupiterDex)

    async def price_side_effect(mint: str) -> float:
        if mint == SOL_MINT:
            return 150.0
        return 0.25

    dex.get_token_price = AsyncMock(side_effect=price_side_effect)
    return dex


@pytest.fixture()
def portfolio(tmp_path: Path, mock_dex: JupiterDex) -> Portfolio:
    return Portfolio(
        rpc_url="https://api.devnet.solana.com",
        jupiter_dex=mock_dex,
        storage_dir=str(tmp_path),
    )


def _make_mock_token_account(pubkey: str, ui_amount: float) -> MagicMock:
    acc = MagicMock()
    acc.pubkey = pubkey
    acc.account.data.parsed = {
        "info": {
            "tokenAmount": {
                "amount": str(int(ui_amount * 1e6)),
                "decimals": 6,
                "uiAmount": ui_amount,
                "uiAmountString": str(ui_amount),
            }
        }
    }
    return acc


class TestGetPortfolioValue:
    async def test_computes_total_value(self, portfolio: Portfolio) -> None:
        mock_sol_resp = MagicMock()
        mock_sol_resp.value = 2_000_000_000  # 2 SOL

        token_accts_resp = MagicMock()
        token_accts_resp.value = [_make_mock_token_account("AcctABC", 100.0)]

        mock_client = AsyncMock()
        mock_client.get_balance = AsyncMock(return_value=mock_sol_resp)
        mock_client.get_token_accounts_by_owner_json_parsed = AsyncMock(
            return_value=token_accts_resp
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "pod_the_trader.trading.portfolio.AsyncClient",
            return_value=mock_client,
        ):
            summary = await portfolio.get_portfolio_value(
                "11111111111111111111111111111111",
                [TEST_MINT],
            )

        assert summary.sol_balance == 2.0
        assert summary.sol_value_usd == 300.0  # 2 * 150
        assert summary.token_balances[TEST_MINT] == 100.0
        assert summary.token_values_usd[TEST_MINT] == 25.0  # 100 * 0.25
        assert summary.total_value_usd == 325.0

    async def test_handles_price_error(self, portfolio: Portfolio) -> None:
        portfolio._dex.get_token_price = AsyncMock(side_effect=Exception("no price"))

        mock_sol_resp = MagicMock()
        mock_sol_resp.value = 1_000_000_000

        mock_client = AsyncMock()
        mock_client.get_balance = AsyncMock(return_value=mock_sol_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "pod_the_trader.trading.portfolio.AsyncClient",
            return_value=mock_client,
        ):
            summary = await portfolio.get_portfolio_value("11111111111111111111111111111111")

        assert summary.sol_balance == 1.0
        assert summary.sol_value_usd == 0.0  # Price fetch failed

    async def test_no_token_mints(self, portfolio: Portfolio) -> None:
        mock_sol_resp = MagicMock()
        mock_sol_resp.value = 500_000_000

        mock_client = AsyncMock()
        mock_client.get_balance = AsyncMock(return_value=mock_sol_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "pod_the_trader.trading.portfolio.AsyncClient",
            return_value=mock_client,
        ):
            summary = await portfolio.get_portfolio_value("11111111111111111111111111111111")

        assert summary.sol_balance == 0.5
        assert summary.token_balances == {}
