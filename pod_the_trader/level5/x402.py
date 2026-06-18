"""UsePod accountless x402 payment path.

UsePod's ``/proxy/x402/v1`` endpoint needs no account, API token, or
dashboard — the local Solana wallet *is* the account and each inference
request is paid per-call over the x402 protocol. Flow (per
docs.usepod.ai/api/x402-payments, which follows the Coinbase exact-SVM
scheme):

  1. Send the inference request with no payment.
  2. Server replies ``402`` with a ``PAYMENT-REQUIRED`` header describing
     the charge: ``quote_id``, ``asset``, ``pay_to``, ``amount_microunits``,
     ``network``, ``mode``.
  3. Client pays on-chain (an SPL token transfer of ``amount_microunits``
     to ``pay_to``'s associated token account) and retries the
     *byte-for-byte identical* request with a ``PAYMENT-SIGNATURE`` header
     (base64 JSON: ``quote_id``, ``network``, ``asset``, ``payer_wallet``,
     ``signature``). The request is bound to a hash of method+path+body, so
     the replay must not differ except for the added header.

Safety: every charge is auto-paid from the trading wallet, so two
config-editable ceilings bound the blast radius — a per-request cap
(reject an oversized/hostile quote) and a per-UTC-day cap (pause
inference once cumulative spend is reached). See ``usepod-x402`` in
config/defaults.yaml.

NOTE: the exact on-chain settlement shape (pay_to as owner vs. ATA,
TransferChecked vs. Transfer, optional memo) follows the canonical
exact-SVM spec. Validate against a single live call before relying on
it for sustained spend — a subtle mismatch means a payment the server
won't credit. The per-request cap bounds that risk.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    TransferCheckedParams,
    get_associated_token_address,
    transfer_checked,
)

if TYPE_CHECKING:
    from solders.keypair import Keypair

logger = logging.getLogger(__name__)

# Solana mainnet CAIP-2 id and canonical USDC mint, per the UsePod x402
# docs. USDC has 6 decimals, so amount_microunits == raw token units.
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


class X402Error(Exception):
    """A non-recoverable x402 payment failure."""


class X402CapExceededError(X402Error):
    """The charge would breach a configured spending cap. Funds NOT sent."""


@dataclass(frozen=True)
class PaymentRequired:
    """Parsed ``PAYMENT-REQUIRED`` 402 challenge."""

    quote_id: str
    asset: str
    pay_to: str
    amount_microunits: int
    network: str
    mode: str = ""

    @property
    def amount_usdc(self) -> float:
        """Charge in USDC dollars (USDC is 6-decimal, so micro == raw)."""
        return self.amount_microunits / (10**USDC_DECIMALS)

    @classmethod
    def parse_header(cls, raw: str | None) -> PaymentRequired:
        """Parse the ``PAYMENT-REQUIRED`` header.

        The value is base64-encoded JSON (x402 convention); we fall back
        to plain JSON in case a deployment sends it unencoded.
        """
        if not raw:
            raise X402Error("402 response carried no PAYMENT-REQUIRED header")
        text = raw.strip()
        data: dict | None = None
        try:
            data = json.loads(base64.b64decode(text).decode())
        except Exception:
            try:
                data = json.loads(text)
            except Exception as e:
                raise X402Error(f"Unparseable PAYMENT-REQUIRED header: {e}") from e
        try:
            return cls(
                quote_id=str(data["quote_id"]),
                asset=str(data["asset"]),
                pay_to=str(data["pay_to"]),
                amount_microunits=int(data["amount_microunits"]),
                network=str(data.get("network", SOLANA_MAINNET)),
                mode=str(data.get("mode", "")),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise X402Error(f"PAYMENT-REQUIRED header missing/invalid fields: {e}") from e


@dataclass
class _DailySpend:
    """UTC-day-scoped cumulative spend tracker."""

    date: str = ""
    spent_usdc: float = 0.0

    def roll(self, today: str) -> None:
        if self.date != today:
            self.date = today
            self.spent_usdc = 0.0


class X402Payer:
    """Builds, caps, and settles x402 charges from the trading wallet."""

    def __init__(
        self,
        keypair: Keypair,
        rpc_url: str,
        *,
        per_request_cap_usdc: float,
        max_daily_spend_usdc: float,
        usdc_mint: str = USDC_MINT,
        confirm_timeout_s: float = 60.0,
    ) -> None:
        self._keypair = keypair
        self._rpc_url = rpc_url
        self._per_request_cap = float(per_request_cap_usdc)
        self._daily_cap = float(max_daily_spend_usdc)
        self._mint = Pubkey.from_string(usdc_mint)
        self._confirm_timeout_s = confirm_timeout_s
        self._daily = _DailySpend()

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    @property
    def spent_today_usdc(self) -> float:
        self._daily.roll(self._today())
        return self._daily.spent_usdc

    def check_caps(self, pr: PaymentRequired) -> None:
        """Raise X402CapExceeded if this charge breaches a cap. No funds move."""
        amount = pr.amount_usdc
        if amount > self._per_request_cap:
            raise X402CapExceededError(
                f"x402 quote ${amount:.6f} exceeds per-request cap "
                f"${self._per_request_cap:.2f} (quote {pr.quote_id}); refusing to pay."
            )
        self._daily.roll(self._today())
        if self._daily.spent_usdc + amount > self._daily_cap:
            raise X402CapExceededError(
                f"x402 daily spend cap ${self._daily_cap:.2f} would be exceeded "
                f"(${self._daily.spent_usdc:.4f} spent + ${amount:.6f}); "
                "pausing inference until the next UTC day."
            )

    async def settle(self, pr: PaymentRequired) -> dict[str, str]:
        """Pay ``pr`` on-chain and return the PAYMENT-SIGNATURE payload.

        Caps are checked BEFORE any funds move. Raises X402Error if the
        transfer can't be confirmed.
        """
        self.check_caps(pr)
        signature = await self._send_transfer(pr)
        # Only record spend once the payment is confirmed on-chain.
        self._daily.roll(self._today())
        self._daily.spent_usdc += pr.amount_usdc
        logger.info(
            "x402 paid $%.6f for quote %s (sig %s); daily total $%.4f/$%.2f",
            pr.amount_usdc,
            pr.quote_id,
            signature[:8],
            self._daily.spent_usdc,
            self._daily_cap,
        )
        return {
            "quote_id": pr.quote_id,
            "network": pr.network,
            "asset": pr.asset,
            "payer_wallet": str(self._keypair.pubkey()),
            "signature": signature,
        }

    async def _send_transfer(self, pr: PaymentRequired) -> str:
        """Build, sign, submit, and confirm the SPL transfer. Returns the sig."""
        from solana.rpc.async_api import AsyncClient

        sender = self._keypair.pubkey()
        source_ata = get_associated_token_address(sender, self._mint)
        dest_owner = Pubkey.from_string(pr.pay_to)
        dest_ata = get_associated_token_address(dest_owner, self._mint)

        ix = transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=source_ata,
                mint=self._mint,
                dest=dest_ata,
                owner=sender,
                amount=pr.amount_microunits,
                decimals=USDC_DECIMALS,
                signers=[],
            )
        )

        async with AsyncClient(self._rpc_url) as client:
            bh = (await client.get_latest_blockhash()).value.blockhash
            msg = MessageV0.try_compile(sender, [ix], [], bh)
            tx = VersionedTransaction(msg, [self._keypair])
            resp = await client.send_raw_transaction(bytes(tx))
            sig = resp.value
            confirmed = await self._confirm(client, sig)
            if not confirmed:
                raise X402Error(
                    f"x402 payment {sig} for quote {pr.quote_id} not confirmed "
                    "on-chain; not retrying the inference request."
                )
            return str(sig)

    async def _confirm(self, client, sig) -> bool:
        """Poll signature status until finalized-or-failed or timeout."""
        import asyncio
        import time

        start = time.monotonic()
        while time.monotonic() - start < self._confirm_timeout_s:
            statuses = (await client.get_signature_statuses([sig])).value
            if statuses and statuses[0] is not None:
                return statuses[0].err is None
            await asyncio.sleep(2)
        return False


class X402Transport(httpx.AsyncBaseTransport):
    """httpx transport that satisfies UsePod's x402 402 challenge inline.

    Wraps an inner transport. On a ``402`` it parses the challenge, pays
    via the injected :class:`X402Payer`, and replays the *identical*
    request with a ``PAYMENT-SIGNATURE`` header. Any other status passes
    straight through. Only one payment attempt is made per request, so a
    server that keeps returning 402 can't trigger repeated charges.
    """

    def __init__(self, payer: X402Payer, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._payer = payer
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if response.status_code != 402:
            return response

        # Drain and close the 402 body before issuing the paid retry.
        await response.aread()
        await response.aclose()

        pr = PaymentRequired.parse_header(response.headers.get("PAYMENT-REQUIRED"))
        payload = await self._payer.settle(pr)  # raises X402CapExceeded on a bad quote
        sig_header = base64.b64encode(json.dumps(payload).encode()).decode()

        # Replay byte-for-byte: same method, URL, body, extensions — only
        # the PAYMENT-SIGNATURE header is added.
        headers = httpx.Headers(request.headers)
        headers["PAYMENT-SIGNATURE"] = sig_header
        paid = httpx.Request(
            request.method,
            request.url,
            headers=headers,
            content=request.content,
            extensions=request.extensions,
        )
        return await self._inner.handle_async_request(paid)

    async def aclose(self) -> None:
        await self._inner.aclose()
