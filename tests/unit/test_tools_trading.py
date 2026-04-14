"""Tests for pod_the_trader.tools.trading_tools."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pod_the_trader.config import Config
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.tools.trading_tools import register_tools
from pod_the_trader.trading.dex import FeasibilityResult, JupiterDex, SwapQuote, TradeExecution
from pod_the_trader.trading.portfolio import Portfolio

SOL_MINT = "So11111111111111111111111111111111111111112"
TEST_MINT = "TokenMintAddress111111111111111111111111111"


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture()
def mock_dex() -> JupiterDex:
    dex = MagicMock(spec=JupiterDex)
    dex.get_quote = AsyncMock(
        return_value=SwapQuote(
            input_mint=SOL_MINT,
            output_mint=TEST_MINT,
            in_amount=100_000_000,
            out_amount=50_000,
            price_impact_pct=0.5,
            slippage_bps=50,
            raw={},
        )
    )
    dex.execute_swap = AsyncMock(
        return_value=TradeExecution(
            success=True,
            signature="swap_sig_123",
            input_mint=SOL_MINT,
            output_mint=TEST_MINT,
            in_amount=100_000_000,
            out_amount=50_000,
        )
    )

    # Per-mint price so the min-trade-size guard behaves realistically:
    # SOL is valuable ($100), USDC is ~$1, TEST_MINT is cheap ($0.15).
    # Existing tests that swap 0.1 SOL see $10 of input value and clear
    # the $1 minimum; the new min-size tests use TEST_MINT at small size.
    async def _price(mint: str) -> float:
        if mint == SOL_MINT:
            return 100.0
        if mint == USDC_MINT:
            return 1.0
        return 0.15

    dex.get_token_price = AsyncMock(side_effect=_price)
    dex.check_feasibility = AsyncMock(
        return_value=FeasibilityResult(feasible=True, price_impact_pct=0.5, reason="OK")
    )
    return dex


@pytest.fixture()
def mock_portfolio(tmp_path: Path) -> Portfolio:
    p = MagicMock(spec=Portfolio)
    p.record_trade = MagicMock()
    # Balance checks used by the new amount-resolution logic
    p.get_sol_balance = AsyncMock(return_value=10.0)
    p.get_token_balance = AsyncMock(return_value=1_000_000.0)
    return p


@pytest.fixture()
def registry(
    sample_config: Config, mock_dex: JupiterDex, mock_portfolio: Portfolio
) -> ToolRegistry:
    # Configure TEST_MINT as the target so the route guard accepts swaps.
    sample_config._set_dotted(sample_config._data, "trading.target_token_address", TEST_MINT)
    reg = ToolRegistry()
    register_tools(
        reg,
        config=sample_config,
        jupiter_dex=mock_dex,
        portfolio=mock_portfolio,
        wallet_address="11111111111111111111111111111111",
    )
    return reg


class TestGetSwapQuote:
    async def test_returns_quote(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": TEST_MINT,
                    "amount_in": 0.1,
                },
            )
        )
        assert result["in_amount_raw"] == 100_000_000
        assert result["out_amount_raw"] == 50_000
        assert "summary" in result


class TestExecuteSwap:
    async def test_returns_error_without_keypair(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "execute_swap",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": TEST_MINT,
                    "amount_in": 0.1,
                },
            )
        )
        assert "error" in result
        assert "keypair" in result["error"].lower()


class TestCheckFeasibility:
    async def test_returns_feasibility(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "check_swap_feasibility",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": TEST_MINT,
                    "amount_in": 0.1,
                },
            )
        )
        assert result["feasible"] is True
        assert result["price_impact_pct"] == 0.5


class TestGetTokenPrice:
    async def test_returns_price(self, registry: ToolRegistry) -> None:
        result = json.loads(await registry.execute("get_token_price", {"mint_address": TEST_MINT}))
        assert result["price_usd"] == 0.15


class TestTradeRouteGuard:
    """The bot is restricted to swaps where each leg is SOL, USDC, or the
    configured target token. Anything else must be refused at the tool
    layer with a clear error.
    """

    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    BOGUS_MINT = "BogusToken1111111111111111111111111111111111"

    async def test_quote_rejects_unknown_input_mint(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": self.BOGUS_MINT,
                    "output_mint": SOL_MINT,
                    "amount_in": 1.0,
                },
            )
        )
        assert "error" in result
        assert "Disallowed swap route" in result["error"]

    async def test_quote_rejects_unknown_output_mint(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": self.BOGUS_MINT,
                    "amount_in": 1.0,
                },
            )
        )
        assert "error" in result
        assert "Disallowed swap route" in result["error"]

    async def test_feasibility_rejects_unknown_mint(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "check_swap_feasibility",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": self.BOGUS_MINT,
                    "amount_in": 1.0,
                },
            )
        )
        assert "error" in result
        assert "Disallowed swap route" in result["error"]

    async def test_quote_accepts_usdc_to_target(self, registry: ToolRegistry) -> None:
        # USDC↔TARGET is explicitly allowed.
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": self.USDC_MINT,
                    "output_mint": TEST_MINT,
                    "amount_in": 1.0,
                },
            )
        )
        assert "error" not in result
        assert "summary" in result

    async def test_quote_accepts_sol_to_usdc(self, registry: ToolRegistry) -> None:
        # SOL↔USDC is also allowed (e.g. converting SOL holdings to USDC).
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": SOL_MINT,
                    "output_mint": self.USDC_MINT,
                    "amount_in": 0.1,
                },
            )
        )
        assert "error" not in result
        assert "summary" in result


class TestMinTradeSizeGuard:
    """Swaps below ``trading.min_trade_size_usdc`` must be rejected at every
    entry point (quote, feasibility, execute) so network fees never exceed
    trade value. The model has produced dust sells of 0.25 tokens worth
    $0.00004 — that should be impossible.
    """

    async def test_execute_rejects_below_minimum(
        self, registry: ToolRegistry, mock_dex: JupiterDex
    ) -> None:
        # mock_dex.get_token_price returns 0.15 (TEST_MINT's "price"). At
        # amount_in=1 that's $0.15 — below the $1 minimum.
        # Need a keypair or execute_swap errors out first.
        from solders.keypair import Keypair

        registry._set_trading_keypair(Keypair())

        result = json.loads(
            await registry.execute(
                "execute_swap",
                {
                    "input_mint": TEST_MINT,
                    "output_mint": SOL_MINT,
                    "amount_in": 1.0,  # $0.15 worth, below $1 minimum
                },
            )
        )
        assert "error" in result
        assert "min_trade_size" in result["error"]
        # The swap must NOT have been executed.
        mock_dex.execute_swap.assert_not_called()

    async def test_quote_rejects_below_minimum(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": TEST_MINT,
                    "output_mint": SOL_MINT,
                    "amount_in": 0.5,  # $0.075 — way below $1
                },
            )
        )
        assert "error" in result
        assert "min_trade_size" in result["error"]

    async def test_feasibility_returns_infeasible_below_minimum(
        self, registry: ToolRegistry
    ) -> None:
        result = json.loads(
            await registry.execute(
                "check_swap_feasibility",
                {
                    "input_mint": TEST_MINT,
                    "output_mint": SOL_MINT,
                    "amount_in": 1.0,  # $0.15
                },
            )
        )
        assert result["feasible"] is False
        assert "min_trade_size" in result["reason"]

    async def test_execute_allows_above_minimum(
        self, registry: ToolRegistry, mock_dex: JupiterDex
    ) -> None:
        from solders.keypair import Keypair

        registry._set_trading_keypair(Keypair())
        # amount_in=10 × $0.15 = $1.50, above the minimum.
        result = json.loads(
            await registry.execute(
                "execute_swap",
                {
                    "input_mint": TEST_MINT,
                    "output_mint": SOL_MINT,
                    "amount_in": 10.0,
                },
            )
        )
        # mock_dex.execute_swap has a TradeExecution return value wired up
        # in the shared fixture, so success should be True.
        assert result.get("success") is True or "error" not in result


class TestSymbolAliasResolution:
    """The model often passes ``"SOL"`` / ``"USDC"`` / ``"<TARGET_SYMBOL>"``
    instead of the base58 mint address. The tool layer accepts these as
    aliases and translates them before the route guard fires.
    """

    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    async def test_symbol_sol_is_accepted(self, registry: ToolRegistry) -> None:
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": "SOL",
                    "output_mint": "USDC",
                    "amount_in": 0.1,
                },
            )
        )
        assert "error" not in result
        assert "summary" in result

    async def test_target_symbol_is_accepted_when_registered(self, registry: ToolRegistry) -> None:
        # Register the target symbol via the late-bound setter.
        registry._set_target_symbol("BOGUS")
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": "USDC",
                    "output_mint": "BOGUS",
                    "amount_in": 1.0,
                },
            )
        )
        assert "error" not in result, result

    async def test_unknown_symbol_falls_through_and_is_rejected(
        self, registry: ToolRegistry
    ) -> None:
        result = json.loads(
            await registry.execute(
                "get_swap_quote",
                {
                    "input_mint": "SOL",
                    "output_mint": "RANDOM_TICKER",
                    "amount_in": 0.1,
                },
            )
        )
        assert "error" in result
        assert "Disallowed swap route" in result["error"]
