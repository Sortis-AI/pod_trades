"""Tests for pod_the_trader.level5.poller.FundingOrchestrator.

The orchestrator coordinates two independent startup waits:

* Level5 funding — polled through Level5Client.get_balance() until the
  account is active or the combined balance crosses a USDC threshold.
* Trading wallet funding — polled through BalancePoller.poll_until_funded
  until the wallet holds enough SOL for Jupiter gas.

These tests drive the orchestrator against mocked Level5 / poller
objects so they don't touch the network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from pod_the_trader.level5.poller import BalancePoller, FundingOrchestrator


def _make_poller(interval: float = 0.01, timeout: float = 0.5) -> BalancePoller:
    """Concrete BalancePoller with a fast interval/timeout. Tests that
    exercise wallet polling swap in their own get_balance stub; tests
    that exercise Level5 polling only care about interval/timeout.
    """
    return BalancePoller(
        rpc_url="https://api.devnet.solana.com",
        wallet_address="11111111111111111111111111111111",
        interval=interval,
        timeout=timeout,
    )


class TestWaitForLevel5Funding:
    async def test_returns_immediately_when_active(self) -> None:
        poller = _make_poller()
        level5 = MagicMock()
        level5.get_balance = AsyncMock(return_value=0.0)
        type(level5).last_is_active = PropertyMock(return_value=True)
        type(level5).last_usdc_balance = PropertyMock(return_value=0.0)
        type(level5).last_credit_balance = PropertyMock(return_value=0.0)

        orch = FundingOrchestrator(poller, level5)
        total = await orch.wait_for_level5_funding(min_usdc=1.0)
        assert total == 0.0
        level5.get_balance.assert_awaited()

    async def test_returns_when_balance_crosses_threshold(self) -> None:
        poller = _make_poller()
        level5 = MagicMock()

        balances = iter([0.0, 0.05, 0.2, 1.5])

        async def _bal() -> float:
            return next(balances)

        level5.get_balance = _bal
        type(level5).last_is_active = PropertyMock(return_value=False)
        type(level5).last_usdc_balance = PropertyMock(return_value=1.5)
        type(level5).last_credit_balance = PropertyMock(return_value=0.0)

        orch = FundingOrchestrator(poller, level5)
        total = await orch.wait_for_level5_funding(min_usdc=1.0)
        assert total == pytest.approx(1.5)

    async def test_times_out_if_never_funded(self) -> None:
        poller = _make_poller(interval=0.01, timeout=0.1)
        level5 = MagicMock()
        level5.get_balance = AsyncMock(return_value=0.0)
        type(level5).last_is_active = PropertyMock(return_value=False)
        type(level5).last_usdc_balance = PropertyMock(return_value=0.0)
        type(level5).last_credit_balance = PropertyMock(return_value=0.0)

        orch = FundingOrchestrator(poller, level5)
        with pytest.raises(TimeoutError, match="Level5 funding timed out"):
            await orch.wait_for_level5_funding(min_usdc=1.0)

    async def test_retries_on_transient_errors(self) -> None:
        poller = _make_poller()
        level5 = MagicMock()

        calls = {"n": 0}

        async def _bal() -> float:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("network blip")
            return 5.0

        level5.get_balance = _bal
        type(level5).last_is_active = PropertyMock(return_value=False)
        type(level5).last_usdc_balance = PropertyMock(return_value=5.0)
        type(level5).last_credit_balance = PropertyMock(return_value=0.0)

        orch = FundingOrchestrator(poller, level5)
        total = await orch.wait_for_level5_funding(min_usdc=1.0)
        assert total == pytest.approx(5.0)
        assert calls["n"] >= 3


class TestWaitForTradingWallet:
    async def test_delegates_to_poller(self) -> None:
        poller = MagicMock()
        poller.poll_until_funded = AsyncMock(return_value=2.0)
        level5 = MagicMock()

        orch = FundingOrchestrator(poller, level5)
        result = await orch.wait_for_trading_wallet(min_sol=0.05)
        assert result == 2.0
        poller.poll_until_funded.assert_awaited_once_with(0.05)
