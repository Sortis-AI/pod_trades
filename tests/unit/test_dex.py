"""Tests for pod_the_trader.trading.dex."""

from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from solders.keypair import Keypair

from pod_the_trader.trading.dex import JupiterDex, JupiterError, SwapQuote

QUOTE_URL = "https://api.jup.ag/swap/v1"
PRICE_URL = "https://lite-api.jup.ag/price/v3"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture()
async def dex():
    async with JupiterDex(
        quote_url=QUOTE_URL,
        swap_url=QUOTE_URL,
        price_url=PRICE_URL,
        rpc_url="https://api.devnet.solana.com",
    ) as d:
        yield d


SAMPLE_QUOTE = {
    "inputMint": SOL_MINT,
    "outputMint": USDC_MINT,
    "inAmount": "1000000000",
    "outAmount": "15000000",
    "priceImpactPct": "0.12",
    "otherAmountThreshold": "14925000",
    "swapMode": "ExactIn",
    "slippageBps": 50,
    "routePlan": [],
}


class TestGetQuote:
    @respx.mock
    async def test_parses_quote(self, dex: JupiterDex) -> None:
        respx.get(f"{QUOTE_URL}/quote").mock(return_value=httpx.Response(200, json=SAMPLE_QUOTE))
        quote = await dex.get_quote(SOL_MINT, USDC_MINT, 1_000_000_000)
        assert isinstance(quote, SwapQuote)
        assert quote.input_mint == SOL_MINT
        assert quote.output_mint == USDC_MINT
        assert quote.in_amount == 1_000_000_000
        assert quote.out_amount == 15_000_000
        assert quote.price_impact_pct == 0.12

    @respx.mock
    async def test_retries_on_failure(self, dex: JupiterDex) -> None:
        route = respx.get(f"{QUOTE_URL}/quote")
        route.side_effect = [
            httpx.Response(500, json={"error": "internal"}),
            httpx.Response(200, json=SAMPLE_QUOTE),
        ]
        quote = await dex.get_quote(SOL_MINT, USDC_MINT, 1_000_000_000)
        assert quote.in_amount == 1_000_000_000
        assert route.call_count == 2

    @respx.mock
    async def test_raises_after_max_retries(self, dex: JupiterDex) -> None:
        respx.get(f"{QUOTE_URL}/quote").mock(
            return_value=httpx.Response(500, json={"error": "down"})
        )
        with pytest.raises(JupiterError, match="failed after"):
            await dex.get_quote(SOL_MINT, USDC_MINT, 1_000_000_000)


class TestExecuteSwap:
    @respx.mock
    async def test_success(self, dex: JupiterDex, mock_keypair: Keypair) -> None:
        respx.get(f"{QUOTE_URL}/quote").mock(return_value=httpx.Response(200, json=SAMPLE_QUOTE))

        # Build a minimal valid VersionedTransaction for the mock swap response
        from solders.hash import Hash
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction

        msg = MessageV0.try_compile(mock_keypair.pubkey(), [], [], Hash.default())
        tx = VersionedTransaction(msg, [mock_keypair])
        import base64

        tx_b64 = base64.b64encode(bytes(tx)).decode()

        respx.post(f"{QUOTE_URL}/swap").mock(
            return_value=httpx.Response(200, json={"swapTransaction": tx_b64})
        )

        mock_send = AsyncMock(return_value="swapsig123")
        mock_confirm = AsyncMock(return_value=True)
        dex._tx_builder.send_versioned_transaction = mock_send
        dex._tx_builder.confirm_transaction = mock_confirm

        result = await dex.execute_swap(mock_keypair, SOL_MINT, USDC_MINT, 1_000_000_000)
        assert result.success is True
        assert result.signature == "swapsig123"

    @respx.mock
    async def test_failure_returns_error(self, dex: JupiterDex, mock_keypair: Keypair) -> None:
        respx.get(f"{QUOTE_URL}/quote").mock(
            return_value=httpx.Response(500, json={"error": "no route"})
        )
        result = await dex.execute_swap(mock_keypair, SOL_MINT, USDC_MINT, 1_000_000_000)
        assert result.success is False
        assert result.error is not None


class TestGetTokenPrice:
    @respx.mock
    async def test_returns_float(self, dex: JupiterDex) -> None:
        respx.get(PRICE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    SOL_MINT: {
                        "usdPrice": 150.25,
                        "priceChange24h": 0.5,
                        "decimals": 9,
                    }
                },
            )
        )
        price = await dex.get_token_price(SOL_MINT)
        assert price == 150.25

    @respx.mock
    async def test_raises_on_missing_data(self, dex: JupiterDex) -> None:
        respx.get(PRICE_URL).mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(JupiterError, match="No price data"):
            await dex.get_token_price(SOL_MINT)


class TestCheckFeasibility:
    @respx.mock
    async def test_feasible(self, dex: JupiterDex) -> None:
        respx.get(f"{QUOTE_URL}/quote").mock(return_value=httpx.Response(200, json=SAMPLE_QUOTE))
        result = await dex.check_feasibility(SOL_MINT, USDC_MINT, 1_000_000_000, max_impact_pct=5.0)
        assert result.feasible is True
        assert result.price_impact_pct == 0.12

    @respx.mock
    async def test_not_feasible_high_impact(self, dex: JupiterDex) -> None:
        high_impact = {**SAMPLE_QUOTE, "priceImpactPct": "8.5"}
        respx.get(f"{QUOTE_URL}/quote").mock(return_value=httpx.Response(200, json=high_impact))
        result = await dex.check_feasibility(SOL_MINT, USDC_MINT, 1_000_000_000, max_impact_pct=5.0)
        assert result.feasible is False
        assert result.price_impact_pct == 8.5
