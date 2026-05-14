"""Tests for pod_the_trader.trading.dex."""

from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from solders.keypair import Keypair

from pod_the_trader.trading.dex import JupiterDex, JupiterError, SwapQuote

QUOTE_URL = "https://api.jup.ag/swap/v1"
PRICE_URL = "https://lite-api.jup.ag/price/v3"
SEARCH_URL = "https://lite-api.jup.ag/tokens/v2/search"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TARGET_MINT = "EN2nnxrg8uUi6x2sJkzNPd2eT6rB9rdSoQNNaENA4RZA"


@pytest.fixture()
async def dex():
    async with JupiterDex(
        quote_url=QUOTE_URL,
        swap_url=QUOTE_URL,
        price_url=PRICE_URL,
        search_url=SEARCH_URL,
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
    async def test_raises_after_max_retries(
        self, dex: JupiterDex, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch the backoff sleep so 5 retries don't slow the suite by
        # ~15s. We're verifying behaviour, not wall-clock timing.
        async def _no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("pod_the_trader.trading.dex.asyncio.sleep", _no_sleep)
        route = respx.get(f"{QUOTE_URL}/quote").mock(
            return_value=httpx.Response(500, json={"error": "down"})
        )
        with pytest.raises(JupiterError, match="failed after"):
            await dex.get_quote(SOL_MINT, USDC_MINT, 1_000_000_000)
        # 5 retries = 1 initial + 5 retries = 6 total HTTP calls
        # (the default since 0.3.3).
        assert route.call_count == 6


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
    async def test_failure_returns_error(
        self,
        dex: JupiterDex,
        mock_keypair: Keypair,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Patch the backoff sleep — with 5 retries × exponential
        # backoff this test would otherwise take ~62s of wall clock.
        async def _no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("pod_the_trader.trading.dex.asyncio.sleep", _no_sleep)
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


class TestGetTokenStats:
    @respx.mock
    async def test_returns_price_and_liquidity(self, dex: JupiterDex) -> None:
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": TARGET_MINT,
                        "symbol": "SQUIRE",
                        "usdPrice": 0.000654,
                        "liquidity": 42000.5,
                    }
                ],
            )
        )
        stats = await dex.get_token_stats(TARGET_MINT)
        assert stats == {"price_usd": 0.000654, "liquidity_usd": 42000.5}

    @respx.mock
    async def test_picks_matching_id_from_multiple_results(self, dex: JupiterDex) -> None:
        # The search endpoint can return multiple tokens matching the query
        # string (symbol fuzzy match). We must pick the one whose `id`
        # exactly matches the requested mint, not the first result.
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "WRONG1111111111111111111111111111111111111",
                        "usdPrice": 99.0,
                        "liquidity": 1.0,
                    },
                    {"id": TARGET_MINT, "usdPrice": 0.001, "liquidity": 5000.0},
                    {
                        "id": "WRONG2222222222222222222222222222222222222",
                        "usdPrice": 99.0,
                        "liquidity": 1.0,
                    },
                ],
            )
        )
        stats = await dex.get_token_stats(TARGET_MINT)
        assert stats["price_usd"] == 0.001
        assert stats["liquidity_usd"] == 5000.0

    @respx.mock
    async def test_missing_liquidity_defaults_to_zero(self, dex: JupiterDex) -> None:
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=[{"id": TARGET_MINT, "usdPrice": 0.001}],
            )
        )
        stats = await dex.get_token_stats(TARGET_MINT)
        assert stats["liquidity_usd"] == 0.0

    @respx.mock
    async def test_null_liquidity_defaults_to_zero(self, dex: JupiterDex) -> None:
        # Jupiter has been observed returning explicit null for tokens
        # without aggregated liquidity data — must not crash on float(None).
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=[{"id": TARGET_MINT, "usdPrice": 0.001, "liquidity": None}],
            )
        )
        stats = await dex.get_token_stats(TARGET_MINT)
        assert stats["liquidity_usd"] == 0.0

    @respx.mock
    async def test_raises_when_mint_not_in_results(self, dex: JupiterDex) -> None:
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=[{"id": "WRONG1111111111111111111111111111111111111", "usdPrice": 99.0}],
            )
        )
        with pytest.raises(JupiterError, match="not found"):
            await dex.get_token_stats(TARGET_MINT)

    @respx.mock
    async def test_raises_on_empty_results(self, dex: JupiterDex) -> None:
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=[]))
        with pytest.raises(JupiterError, match="not found"):
            await dex.get_token_stats(TARGET_MINT)

    @respx.mock
    async def test_raises_on_missing_price(self, dex: JupiterDex) -> None:
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=[{"id": TARGET_MINT, "liquidity": 5000.0}],
            )
        )
        with pytest.raises(JupiterError, match="No usdPrice"):
            await dex.get_token_stats(TARGET_MINT)


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
