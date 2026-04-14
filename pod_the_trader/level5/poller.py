"""Startup funding waits.

Two things need to happen before the bot can trade:

1. **The trading wallet needs SOL** for Jupiter gas. The operator sends
   SOL directly to the bot's generated keypair on-chain; we poll the
   RPC until it shows up.
2. **The Level5 account needs a credited balance** to pay for LLM
   inference. The operator funds Level5 through the dashboard
   (``https://level5.cloud/dashboard/<api_token>``) — it's a USDC
   deposit to a sovereign contract routed via the account's
   ``deposit_code``. pod-the-trader has no programmatic deposit
   path; per Level5 SKILL v1.7.2 the dashboard is the supported
   flow. We poll ``/proxy/{token}/balance`` and proceed once
   ``is_active`` flips true or the combined USDC+credit balance
   crosses a configurable floor.

The previous version of this module tried to auto-deposit SOL to the
Level5 contract. That flow was conceptually broken — Level5 bills in
USDC and routes by ``deposit_code``, not by sender address — so a SOL
transfer to the contract address did nothing useful. That code is
gone; operators fund Level5 through the dashboard instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

if TYPE_CHECKING:
    from pod_the_trader.level5.client import Level5Client

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


class BalancePoller:
    """Polls Solana RPC for a wallet's SOL balance."""

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

    @property
    def interval(self) -> float:
        return self._interval

    @property
    def timeout(self) -> float:
        return self._timeout

    async def get_balance(self) -> float:
        """Fetch current SOL balance in whole SOL units."""
        async with AsyncClient(self._rpc_url) as client:
            resp = await client.get_balance(Pubkey.from_string(self._wallet_address))
            return resp.value / LAMPORTS_PER_SOL

    async def poll_until_funded(self, threshold_sol: float) -> float:
        """Poll until the wallet holds at least ``threshold_sol`` SOL.

        Returns the first observed balance at or above the threshold.
        Raises ``TimeoutError`` if funding doesn't arrive within
        :attr:`timeout` seconds.
        """
        start = time.monotonic()
        last_balance = -1.0

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= self._timeout:
                raise TimeoutError(
                    f"Trading wallet funding timed out after {self._timeout:.0f}s. "
                    f"Last balance: {last_balance:.6f} SOL, "
                    f"threshold: {threshold_sol:.6f} SOL"
                )

            balance = await self.get_balance()
            if balance != last_balance:
                logger.info("Trading wallet balance: %.6f SOL", balance)
                last_balance = balance

            if balance >= threshold_sol:
                return balance

            await asyncio.sleep(self._interval)


class FundingOrchestrator:
    """Coordinates the two startup waits: Level5 funding + wallet SOL.

    This is the public entry point pod-the-trader uses after Level5
    registration. Both waits run with the same poll interval and
    timeout as the wallet poller so the operator sees a single
    consistent cadence. Level5 funding completes when the account goes
    active (first deposit lands) or the combined balance crosses the
    configured minimum; wallet funding completes when the trading
    wallet holds enough SOL for Jupiter gas.
    """

    def __init__(
        self,
        poller: BalancePoller,
        level5_client: Level5Client,
    ) -> None:
        self._poller = poller
        self._level5 = level5_client

    async def wait_for_level5_funding(self, min_usdc: float) -> float:
        """Poll Level5's ``/balance`` endpoint until the account is usable.

        Returns the combined balance (USDC + promotional credits) when
        the account is considered funded. Raises ``TimeoutError`` on
        timeout. "Usable" means either:

        * Level5 reports ``is_active: true`` — the sovereign contract
          has credited the first deposit (or SQUIRE lock), OR
        * The combined USDC+credit balance is ≥ ``min_usdc``.

        The first condition catches the common case where the operator
        makes a small initial deposit to activate the account; the
        second catches wallets that were pre-funded.
        """
        start = time.monotonic()
        last_active = False
        last_total = -1.0

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= self._poller.timeout:
                raise TimeoutError(
                    f"Level5 funding timed out after {self._poller.timeout:.0f}s. "
                    f"Last total: ${last_total:.6f} USDC, "
                    f"active: {last_active}, threshold: ${min_usdc:.6f}"
                )

            try:
                total = await self._level5.get_balance()
                is_active = self._level5.last_is_active
            except Exception as e:
                logger.debug("Level5 balance poll failed: %s (retrying)", e)
                await asyncio.sleep(self._poller.interval)
                continue

            if total != last_total or is_active != last_active:
                logger.info(
                    "Level5 balance: $%.6f (usdc=$%.6f credits=$%.6f active=%s)",
                    total,
                    self._level5.last_usdc_balance,
                    self._level5.last_credit_balance,
                    is_active,
                )
                last_total = total
                last_active = is_active

            if is_active or total >= min_usdc:
                return total

            await asyncio.sleep(self._poller.interval)

    async def wait_for_trading_wallet(self, min_sol: float) -> float:
        """Poll the trading wallet until it holds at least ``min_sol`` SOL.

        Thin wrapper around :meth:`BalancePoller.poll_until_funded` so
        the orchestrator has a single surface for both waits.
        """
        return await self._poller.poll_until_funded(min_sol)
