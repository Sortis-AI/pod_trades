"""Tests for pod_the_trader.level5.poller."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pod_the_trader.level5.poller import BalancePoller


class TestBalancePoller:
    @pytest.fixture()
    def poller(self) -> BalancePoller:
        return BalancePoller(
            rpc_url="https://api.devnet.solana.com",
            wallet_address="11111111111111111111111111111111",
            interval=0.01,
            timeout=0.5,
        )

    async def test_get_balance_converts_lamports(self, poller: BalancePoller) -> None:
        mock_resp = MagicMock()
        mock_resp.value = 2_500_000_000  # 2.5 SOL

        mock_client = AsyncMock()
        mock_client.get_balance = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("pod_the_trader.level5.poller.AsyncClient", return_value=mock_client):
            balance = await poller.get_balance()
        assert balance == 2.5

    async def test_poll_returns_when_funded(self, poller: BalancePoller) -> None:
        call_count = 0

        async def mock_get_balance() -> float:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return 0.0
            return 1.0

        poller.get_balance = mock_get_balance  # type: ignore[assignment]
        balance = await poller.poll_until_funded(0.5)
        assert balance == 1.0
        assert call_count == 3

    async def test_poll_times_out(self, poller: BalancePoller) -> None:
        async def always_zero() -> float:
            return 0.0

        poller.get_balance = always_zero  # type: ignore[assignment]
        with pytest.raises(TimeoutError, match="timed out"):
            await poller.poll_until_funded(1.0)

    async def test_balance_change_callback(self, poller: BalancePoller) -> None:
        balances = [0.0, 0.0, 0.5, 1.0]
        idx = 0
        changes: list[float] = []

        async def mock_get_balance() -> float:
            nonlocal idx
            val = balances[min(idx, len(balances) - 1)]
            idx += 1
            return val

        poller.get_balance = mock_get_balance  # type: ignore[assignment]
        await poller.poll_until_funded(1.0, on_balance_change=lambda b: changes.append(b))
        assert 0.5 in changes
        assert 1.0 in changes
