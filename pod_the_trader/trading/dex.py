"""Jupiter DEX aggregator integration."""

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from pod_the_trader.trading.transaction import TransactionBuilder

logger = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@dataclass
class SwapQuote:
    """Parsed Jupiter quote response."""

    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float
    slippage_bps: int
    raw: dict


@dataclass
class TradeExecution:
    """Result of a swap execution."""

    success: bool
    signature: str | None = None
    input_mint: str = ""
    output_mint: str = ""
    in_amount: int = 0  # raw lamports/atoms requested
    out_amount: int = 0  # raw atoms quoted as expected output
    actual_out_amount: int = 0  # raw atoms actually received (from chain)
    gas_lamports: int = 0  # network fee paid
    block_slot: int = 0
    block_time: int = 0
    price_impact_pct: float = 0.0
    slippage_bps_requested: int = 0
    error: str | None = None


@dataclass
class FeasibilityResult:
    """Assessment of whether a swap is feasible."""

    feasible: bool
    price_impact_pct: float
    reason: str


class JupiterError(Exception):
    """Raised on Jupiter API failures."""


class JupiterDex:
    """Client for the Jupiter DEX aggregator.

    Use as an async context manager:
        async with JupiterDex(...) as dex:
            quote = await dex.get_quote(...)
    """

    def __init__(
        self,
        quote_url: str = "https://api.jup.ag/swap/v1",
        swap_url: str = "https://api.jup.ag/swap/v1",
        price_url: str = "https://lite-api.jup.ag/price/v3",
        rpc_url: str = "https://api.mainnet-beta.solana.com",
    ) -> None:
        self._quote_url = quote_url.rstrip("/")
        self._swap_url = swap_url.rstrip("/")
        self._price_url = price_url.rstrip("/")
        self._rpc_url = rpc_url
        self._http: httpx.AsyncClient | None = None
        self._tx_builder = TransactionBuilder(rpc_url)

    async def __aenter__(self) -> "JupiterDex":
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=30, write=30, pool=30),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise JupiterError("JupiterDex must be used as an async context manager")
        return self._http

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: int = 50,
    ) -> SwapQuote:
        """Fetch a swap quote from Jupiter."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": str(slippage_bps),
        }
        data = await self._request_with_retry("GET", f"{self._quote_url}/quote", params=params)
        return SwapQuote(
            input_mint=data["inputMint"],
            output_mint=data["outputMint"],
            in_amount=int(data["inAmount"]),
            out_amount=int(data["outAmount"]),
            price_impact_pct=float(data.get("priceImpactPct", 0)),
            slippage_bps=slippage_bps,
            raw=data,
        )

    async def execute_swap(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: int = 50,
    ) -> TradeExecution:
        """Execute a full swap: quote -> swap tx -> sign -> submit -> confirm."""
        try:
            quote = await self.get_quote(input_mint, output_mint, amount_lamports, slippage_bps)

            swap_data = await self._request_with_retry(
                "POST",
                f"{self._swap_url}/swap",
                json_data={
                    "quoteResponse": quote.raw,
                    "userPublicKey": str(keypair.pubkey()),
                },
            )

            raw_tx = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(tx.message, [keypair])

            sig = await self._tx_builder.send_versioned_transaction(signed_tx)
            confirmed = await self._tx_builder.confirm_transaction(sig)

            if not confirmed:
                return TradeExecution(
                    success=False,
                    signature=sig,
                    input_mint=input_mint,
                    output_mint=output_mint,
                    in_amount=quote.in_amount,
                    out_amount=quote.out_amount,
                    price_impact_pct=quote.price_impact_pct,
                    slippage_bps_requested=slippage_bps,
                    error="Transaction not confirmed within timeout",
                )

            # Enrich with on-chain data: gas fee + actual amounts
            enrichment = await self._tx_builder.fetch_swap_details(
                sig, str(keypair.pubkey()), output_mint
            )

            return TradeExecution(
                success=True,
                signature=sig,
                input_mint=input_mint,
                output_mint=output_mint,
                in_amount=quote.in_amount,
                out_amount=quote.out_amount,
                actual_out_amount=enrichment.get("actual_out_raw", 0),
                gas_lamports=enrichment.get("gas_lamports", 0),
                block_slot=enrichment.get("block_slot", 0),
                block_time=enrichment.get("block_time", 0),
                price_impact_pct=quote.price_impact_pct,
                slippage_bps_requested=slippage_bps,
            )
        except Exception as e:
            logger.error("Swap execution failed: %s", e)
            return TradeExecution(
                success=False,
                input_mint=input_mint,
                output_mint=output_mint,
                error=str(e),
            )

    async def get_token_price(self, mint_address: str) -> float:
        """Get current USD price for a token via Jupiter Price API v3.

        Response format: {mint: {usdPrice, priceChange24h, ...}}
        """
        data = await self._request_with_retry(
            "GET",
            self._price_url,
            params={"ids": mint_address},
        )
        token_data = data.get(mint_address)
        if not token_data or "usdPrice" not in token_data:
            raise JupiterError(f"No price data for {mint_address}")
        return float(token_data["usdPrice"])

    async def check_feasibility(
        self,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        max_impact_pct: float = 5.0,
    ) -> FeasibilityResult:
        """Check if a swap is feasible given price impact constraints."""
        try:
            quote = await self.get_quote(input_mint, output_mint, amount_lamports)
            impact = quote.price_impact_pct

            if impact > max_impact_pct:
                return FeasibilityResult(
                    feasible=False,
                    price_impact_pct=impact,
                    reason=f"Price impact {impact:.2f}% exceeds max {max_impact_pct:.2f}%",
                )

            return FeasibilityResult(
                feasible=True,
                price_impact_pct=impact,
                reason=f"Price impact {impact:.2f}% is within acceptable range",
            )
        except Exception as e:
            return FeasibilityResult(
                feasible=False,
                price_impact_pct=0.0,
                reason=f"Feasibility check failed: {e}",
            )

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_data: dict | None = None,
        max_retries: int = 3,
    ) -> dict:
        """HTTP request with retry on failure."""
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                response = await self._client.request(method, url, params=params, json=json_data)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait = 2**attempt
                    logger.warning(
                        "Jupiter %s %s failed, retrying in %ds (attempt %d/%d): %s",
                        method,
                        url,
                        wait,
                        attempt + 1,
                        max_retries,
                        e,
                    )
                    await asyncio.sleep(wait)

        raise JupiterError(
            f"Jupiter request failed after {max_retries} attempts: {last_error}"
        ) from last_error
