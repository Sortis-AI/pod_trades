"""Tests for pod_the_trader.level5.poller.FundingOrchestrator."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from solders.keypair import Keypair

from pod_the_trader.level5.poller import FundingOrchestrator


@pytest.fixture()
def orchestrator() -> FundingOrchestrator:
    poller = MagicMock()
    poller.poll_until_funded = AsyncMock(return_value=1.0)
    level5 = MagicMock()
    tx_builder = MagicMock()
    tx_builder.transfer_sol = AsyncMock(return_value="sig123")
    tx_builder.confirm_transaction = AsyncMock(return_value=True)
    return FundingOrchestrator(poller, level5, tx_builder)


class TestWaitAndDeposit:
    async def test_success(self, orchestrator: FundingOrchestrator) -> None:
        kp = Keypair()
        result = await orchestrator.wait_and_deposit(
            keypair=kp,
            deposit_address="DepAddr",
            deposit_amount_sol=0.1,
            funding_threshold_sol=0.5,
        )
        assert result is True

    async def test_not_confirmed(self, orchestrator: FundingOrchestrator) -> None:
        orchestrator._tx_builder.confirm_transaction = AsyncMock(return_value=False)
        kp = Keypair()
        result = await orchestrator.wait_and_deposit(
            keypair=kp,
            deposit_address="DepAddr",
            deposit_amount_sol=0.1,
            funding_threshold_sol=0.5,
        )
        assert result is False

    async def test_transfer_failure(self, orchestrator: FundingOrchestrator) -> None:
        orchestrator._tx_builder.transfer_sol = AsyncMock(side_effect=Exception("tx failed"))
        kp = Keypair()
        result = await orchestrator.wait_and_deposit(
            keypair=kp,
            deposit_address="DepAddr",
            deposit_amount_sol=0.1,
            funding_threshold_sol=0.5,
        )
        assert result is False
