"""Balance polling and auto-deposit orchestration."""

import asyncio
import logging
import time
from collections.abc import Callable

from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


class BalancePoller:
    """Polls Solana RPC for wallet SOL balance."""

    def __init__(
        self,
        rpc_url: str,
        wallet_address: str,
        interval: float = 10.0,
        timeout: float = 3600.0,
    ) -> None:
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._interval = interval
        self._timeout = timeout

    async def get_balance(self) -> float:
        """Fetch current SOL balance."""
        async with AsyncClient(self._rpc_url) as client:
            resp = await client.get_balance(Pubkey.from_string(self._wallet_address))
            return resp.value / LAMPORTS_PER_SOL

    async def poll_until_funded(
        self,
        threshold_sol: float,
        on_balance_change: Callable[[float], None] | None = None,
    ) -> float:
        """Poll until balance meets threshold. Returns the balance.

        Raises TimeoutError if funding doesn't arrive within the configured timeout.
        """
        start = time.monotonic()
        last_balance = -1.0

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= self._timeout:
                raise TimeoutError(
                    f"Wallet funding timed out after {self._timeout:.0f}s. "
                    f"Last balance: {last_balance:.6f} SOL, threshold: {threshold_sol:.6f} SOL"
                )

            balance = await self.get_balance()

            if balance != last_balance:
                logger.info("Wallet balance: %.6f SOL", balance)
                if on_balance_change and last_balance >= 0:
                    on_balance_change(balance)
                last_balance = balance

            if balance >= threshold_sol:
                return balance

            await asyncio.sleep(self._interval)


class FundingOrchestrator:
    """Coordinates wait-for-funding and auto-deposit to Level5."""

    def __init__(
        self,
        poller: BalancePoller,
        level5_client: "Level5Client",  # noqa: F821 — forward ref, wired at runtime
        transaction_builder: "TransactionBuilder",  # noqa: F821
    ) -> None:
        self._poller = poller
        self._level5 = level5_client
        self._tx_builder = transaction_builder

    async def wait_and_deposit(
        self,
        keypair: Keypair,
        deposit_address: str,
        deposit_amount_sol: float,
        funding_threshold_sol: float,
    ) -> bool:
        """Wait for the wallet to be funded, then deposit to Level5.

        Returns True on successful deposit, False on failure.
        """
        logger.info(
            "Waiting for wallet to be funded with at least %.4f SOL...",
            funding_threshold_sol,
        )
        balance = await self._poller.poll_until_funded(funding_threshold_sol)
        logger.info(
            "Wallet funded with %.6f SOL. Depositing %.4f SOL to Level5...",
            balance,
            deposit_amount_sol,
        )

        try:
            sig = await self._tx_builder.transfer_sol(keypair, deposit_address, deposit_amount_sol)
            logger.info("Deposit transaction sent: %s", sig)
            confirmed = await self._tx_builder.confirm_transaction(sig)
            if confirmed:
                logger.info("Deposit confirmed successfully")
            else:
                logger.warning("Deposit transaction not confirmed within timeout")
            return confirmed
        except Exception as e:
            logger.error("Deposit failed: %s", e)
            return False
