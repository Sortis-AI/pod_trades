"""Tests for pod_the_trader.trading.portfolio."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pod_the_trader.trading.dex import JupiterDex
from pod_the_trader.trading.portfolio import (
    Portfolio,
    TradeRecord,
)

SOL_MINT = "So11111111111111111111111111111111111111112"
TEST_MINT = "TokenMintAddress111111111111111111111111111"


@pytest.fixture()
def mock_dex() -> JupiterDex:
    dex = MagicMock(spec=JupiterDex)
    dex.get_token_price = AsyncMock(return_value=150.0)
    return dex


@pytest.fixture()
def portfolio(tmp_path: Path, mock_dex: JupiterDex) -> Portfolio:
    return Portfolio(
        rpc_url="https://api.devnet.solana.com",
        jupiter_dex=mock_dex,
        storage_dir=str(tmp_path),
    )


class TestGetSolBalance:
    async def test_returns_correct_amount(self, portfolio: Portfolio) -> None:
        mock_resp = MagicMock()
        mock_resp.value = 5_000_000_000  # 5 SOL

        mock_client = AsyncMock()
        mock_client.get_balance = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("pod_the_trader.trading.portfolio.AsyncClient", return_value=mock_client):
            balance = await portfolio.get_sol_balance("11111111111111111111111111111111")

        assert balance == 5.0


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


class TestGetTokenBalance:
    async def test_returns_balance(self, portfolio: Portfolio) -> None:
        acc = _make_mock_token_account("TokenAcctABC", 1000.5)
        resp = MagicMock()
        resp.value = [acc]

        mock_client = AsyncMock()
        mock_client.get_token_accounts_by_owner_json_parsed = AsyncMock(return_value=resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("pod_the_trader.trading.portfolio.AsyncClient", return_value=mock_client):
            balance = await portfolio.get_token_balance(
                "11111111111111111111111111111111", TEST_MINT
            )

        # RPC returns the same account under both program filters; dedupe
        # keeps only one, so the result should be 1000.5 not 2001.0
        assert balance == 1000.5

    async def test_returns_zero_when_no_account(self, portfolio: Portfolio) -> None:
        empty_resp = MagicMock()
        empty_resp.value = []
        # The ATA fallback path queries get_token_account_balance — return
        # a value with ui_amount=None to simulate "no balance".
        ata_resp = MagicMock()
        ata_resp.value.ui_amount = None
        mock_client = AsyncMock()
        mock_client.get_token_accounts_by_owner_json_parsed = AsyncMock(return_value=empty_resp)
        mock_client.get_token_account_balance = AsyncMock(return_value=ata_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("pod_the_trader.trading.portfolio.AsyncClient", return_value=mock_client):
            balance = await portfolio.get_token_balance(
                "11111111111111111111111111111111", TEST_MINT
            )

        assert balance == 0.0

    async def test_dedup_across_programs(self, portfolio: Portfolio) -> None:
        """Same account pubkey returned under both program queries — dedupe."""
        acc = _make_mock_token_account("SameAccount", 500.0)
        resp = MagicMock()
        resp.value = [acc]

        mock_client = AsyncMock()
        mock_client.get_token_accounts_by_owner_json_parsed = AsyncMock(return_value=resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("pod_the_trader.trading.portfolio.AsyncClient", return_value=mock_client):
            balance = await portfolio.get_token_balance(
                "11111111111111111111111111111111", TEST_MINT
            )

        # Returned once under each program filter, same pubkey, dedup = 500
        assert balance == 500.0

    async def test_token_2022_program(self, portfolio: Portfolio) -> None:
        """Token found only under Token-2022 program (different pubkey)."""
        legacy_empty = MagicMock()
        legacy_empty.value = []
        token2022_resp = MagicMock()
        token2022_resp.value = [_make_mock_token_account("T22Acct", 777.0)]

        mock_client = AsyncMock()
        # First call: legacy program (empty), second: Token-2022 (has account)
        mock_client.get_token_accounts_by_owner_json_parsed = AsyncMock(
            side_effect=[legacy_empty, token2022_resp]
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("pod_the_trader.trading.portfolio.AsyncClient", return_value=mock_client):
            balance = await portfolio.get_token_balance(
                "11111111111111111111111111111111", TEST_MINT
            )

        assert balance == 777.0


class TestRecordAndHistory:
    def test_record_trade_appends(self, portfolio: Portfolio) -> None:
        trade = TradeRecord(
            timestamp="2026-04-01T10:00:00Z",
            side="buy",
            input_mint=SOL_MINT,
            output_mint=TEST_MINT,
            input_amount=1.0,
            output_amount=100.0,
            price_usd=0.15,
            value_usd=15.0,
            signature="sig1",
        )
        portfolio.record_trade(trade)
        history = portfolio.get_trade_history()
        assert len(history) == 1
        assert history[0].side == "buy"
        assert history[0].signature == "sig1"

    def test_record_multiple_trades(self, portfolio: Portfolio) -> None:
        for i in range(5):
            portfolio.record_trade(
                TradeRecord(
                    timestamp=f"2026-04-0{i + 1}T10:00:00Z",
                    side="buy" if i % 2 == 0 else "sell",
                    input_mint=SOL_MINT,
                    output_mint=TEST_MINT,
                    input_amount=1.0,
                    output_amount=100.0,
                    price_usd=0.15,
                    value_usd=15.0,
                    signature=f"sig{i}",
                )
            )
        assert len(portfolio.get_trade_history()) == 5

    def test_history_limit(self, portfolio: Portfolio) -> None:
        for i in range(10):
            portfolio.record_trade(
                TradeRecord(
                    timestamp=f"2026-04-0{i + 1}T10:00:00Z",
                    side="buy",
                    input_mint=SOL_MINT,
                    output_mint=TEST_MINT,
                    input_amount=1.0,
                    output_amount=100.0,
                    price_usd=0.15,
                    value_usd=15.0,
                    signature=f"sig{i}",
                )
            )
        assert len(portfolio.get_trade_history(limit=3)) == 3


class TestPnL:
    def test_empty_history(self, portfolio: Portfolio) -> None:
        pnl = portfolio.calculate_pnl()
        assert pnl.total_pnl_usd == 0.0
        assert pnl.total_trades == 0
        assert pnl.win_rate == 0.0

    def test_buy_sell_pair_profit(self, portfolio: Portfolio) -> None:
        portfolio.record_trade(
            TradeRecord(
                timestamp="2026-04-01T10:00:00Z",
                side="buy",
                input_mint=SOL_MINT,
                output_mint=TEST_MINT,
                input_amount=1.0,
                output_amount=100.0,
                price_usd=0.15,
                value_usd=15.0,
                signature="buy1",
            )
        )
        portfolio.record_trade(
            TradeRecord(
                timestamp="2026-04-02T10:00:00Z",
                side="sell",
                input_mint=TEST_MINT,
                output_mint=SOL_MINT,
                input_amount=100.0,
                output_amount=1.2,
                price_usd=0.18,
                value_usd=18.0,
                signature="sell1",
            )
        )
        pnl = portfolio.calculate_pnl()
        assert pnl.total_pnl_usd == pytest.approx(3.0)
        assert pnl.win_rate == 100.0
        assert pnl.total_trades == 2
        assert pnl.largest_win == pytest.approx(3.0)

    def test_buy_sell_pair_loss(self, portfolio: Portfolio) -> None:
        portfolio.record_trade(
            TradeRecord(
                timestamp="2026-04-01T10:00:00Z",
                side="buy",
                input_mint=SOL_MINT,
                output_mint=TEST_MINT,
                input_amount=1.0,
                output_amount=100.0,
                price_usd=0.15,
                value_usd=15.0,
                signature="buy1",
            )
        )
        portfolio.record_trade(
            TradeRecord(
                timestamp="2026-04-02T10:00:00Z",
                side="sell",
                input_mint=TEST_MINT,
                output_mint=SOL_MINT,
                input_amount=100.0,
                output_amount=0.8,
                price_usd=0.12,
                value_usd=12.0,
                signature="sell1",
            )
        )
        pnl = portfolio.calculate_pnl()
        assert pnl.total_pnl_usd == pytest.approx(-3.0)
        assert pnl.win_rate == 0.0
        assert pnl.largest_loss == pytest.approx(-3.0)

    def test_mixed_results(self, portfolio: Portfolio) -> None:
        # Win: buy 10, sell 15 = +5
        portfolio.record_trade(
            TradeRecord("2026-04-01", "buy", SOL_MINT, TEST_MINT, 1.0, 100.0, 0.1, 10.0, "b1")
        )
        portfolio.record_trade(
            TradeRecord("2026-04-02", "sell", TEST_MINT, SOL_MINT, 100.0, 1.5, 0.15, 15.0, "s1")
        )
        # Loss: buy 20, sell 12 = -8
        portfolio.record_trade(
            TradeRecord("2026-04-03", "buy", SOL_MINT, TEST_MINT, 2.0, 200.0, 0.1, 20.0, "b2")
        )
        portfolio.record_trade(
            TradeRecord("2026-04-04", "sell", TEST_MINT, SOL_MINT, 200.0, 1.2, 0.06, 12.0, "s2")
        )

        pnl = portfolio.calculate_pnl()
        assert pnl.total_pnl_usd == pytest.approx(-3.0)
        assert pnl.win_rate == 50.0
        assert pnl.total_trades == 4
        assert pnl.largest_win == pytest.approx(5.0)
        assert pnl.largest_loss == pytest.approx(-8.0)
