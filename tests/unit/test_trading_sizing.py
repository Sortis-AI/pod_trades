"""Tests for swap sizing: decimals lookup, amount_in, percent_of_balance."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from solders.keypair import Keypair

from pod_the_trader.config import Config
from pod_the_trader.data.ledger import TradeLedger
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.tools.trading_tools import (
    _DECIMALS_CACHE,
    _fetch_decimals,
    _resolve_amount_raw,
    register_tools,
)
from pod_the_trader.trading.dex import FeasibilityResult, JupiterDex, SwapQuote, TradeExecution
from pod_the_trader.trading.portfolio import Portfolio

SOL_MINT = "So11111111111111111111111111111111111111112"
SQUIRE_MINT = "EN2nnxrg8uUi6x2sJkzNPd2eT6rB9rdSoQNNaENA4RZA"


@pytest.fixture(autouse=True)
def _reset_decimals_cache():
    """Reset the module-level decimals cache between tests."""
    _DECIMALS_CACHE.clear()
    _DECIMALS_CACHE[SOL_MINT] = 9
    yield
    _DECIMALS_CACHE.clear()
    _DECIMALS_CACHE[SOL_MINT] = 9


class TestDecimalsLookup:
    async def test_sol_is_cached(self) -> None:
        assert await _fetch_decimals(SOL_MINT) == 9

    async def test_fetches_and_caches_spl(self) -> None:
        with patch("pod_the_trader.tools.trading_tools.httpx.AsyncClient") as mock_http_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": SQUIRE_MINT, "decimals": 6}]
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_http_cls.return_value = mock_client

            assert await _fetch_decimals(SQUIRE_MINT) == 6
            assert _DECIMALS_CACHE[SQUIRE_MINT] == 6

            # Second call should hit cache, not HTTP
            assert await _fetch_decimals(SQUIRE_MINT) == 6

    async def test_defaults_to_6_on_failure(self) -> None:
        with patch("pod_the_trader.tools.trading_tools.httpx.AsyncClient") as mock_http_cls:
            mock_http_cls.side_effect = Exception("network down")
            assert await _fetch_decimals("SomeUnknownMint") == 6


class TestResolveAmount:
    @pytest.fixture()
    def portfolio(self) -> Portfolio:
        p = MagicMock(spec=Portfolio)
        p.get_sol_balance = AsyncMock(return_value=1.0)
        p.get_token_balance = AsyncMock(return_value=500_000.0)
        return p

    async def test_amount_in_sol(self, portfolio: Portfolio) -> None:
        raw, decimals, ui, err = await _resolve_amount_raw(
            {"amount_in": 0.1},
            SOL_MINT,
            portfolio,
            "wallet",
        )
        assert err is None
        assert decimals == 9
        assert ui == 0.1
        # 0.1 * 1e9 = 100,000,000 lamports
        assert raw == 100_000_000

    async def test_amount_in_spl_token(self, portfolio: Portfolio) -> None:
        _DECIMALS_CACHE[SQUIRE_MINT] = 6
        raw, decimals, ui, err = await _resolve_amount_raw(
            {"amount_in": 200_000.0},
            SQUIRE_MINT,
            portfolio,
            "wallet",
        )
        assert err is None
        assert decimals == 6
        # 200000 * 1e6 = 2e11 raw atoms
        assert raw == 200_000_000_000
        assert ui == 200_000.0

    async def test_amount_in_exceeds_balance(self, portfolio: Portfolio) -> None:
        _DECIMALS_CACHE[SQUIRE_MINT] = 6
        raw, _decimals, _ui, err = await _resolve_amount_raw(
            {"amount_in": 1_000_000.0},  # wallet has 500k
            SQUIRE_MINT,
            portfolio,
            "wallet",
        )
        assert err is not None
        assert "exceeds wallet balance" in err
        assert raw == 0

    async def test_sol_amount_reserves_gas(self, portfolio: Portfolio) -> None:
        # wallet has 1.0 SOL, try to use 1.0 → should fail (reserve 0.01)
        raw, _decimals, _ui, err = await _resolve_amount_raw(
            {"amount_in": 1.0},
            SOL_MINT,
            portfolio,
            "wallet",
        )
        assert err is not None
        assert "reserves 0.01 SOL" in err

    async def test_percent_of_balance_spl(self, portfolio: Portfolio) -> None:
        _DECIMALS_CACHE[SQUIRE_MINT] = 6
        # 50% of 500,000 = 250,000 tokens
        raw, decimals, ui, err = await _resolve_amount_raw(
            {"percent_of_balance": 50.0},
            SQUIRE_MINT,
            portfolio,
            "wallet",
        )
        assert err is None
        assert ui == pytest.approx(250_000.0)
        assert raw == 250_000_000_000

    async def test_percent_of_balance_sol_reserves_gas(self, portfolio: Portfolio) -> None:
        # 100% of 1.0 SOL - 0.01 reserve = 0.99 SOL
        raw, _decimals, ui, err = await _resolve_amount_raw(
            {"percent_of_balance": 100.0},
            SOL_MINT,
            portfolio,
            "wallet",
        )
        assert err is None
        assert ui == pytest.approx(0.99)

    async def test_percent_out_of_range(self, portfolio: Portfolio) -> None:
        _, _, _, err = await _resolve_amount_raw(
            {"percent_of_balance": 150.0},
            SQUIRE_MINT,
            portfolio,
            "wallet",
        )
        assert err is not None
        assert "must be in" in err

    async def test_amount_in_raw_bypasses_decimals(self, portfolio: Portfolio) -> None:
        _DECIMALS_CACHE[SQUIRE_MINT] = 6
        raw, _decimals, _ui, err = await _resolve_amount_raw(
            {"amount_in_raw": 500_000_000},
            SQUIRE_MINT,
            portfolio,
            "wallet",
        )
        assert err is None
        assert raw == 500_000_000

    async def test_no_amount_specified(self, portfolio: Portfolio) -> None:
        _, _, _, err = await _resolve_amount_raw(
            {},
            SOL_MINT,
            portfolio,
            "wallet",
        )
        assert err is not None
        assert "amount_in" in err

    async def test_legacy_amount_sol_still_works(self, portfolio: Portfolio) -> None:
        """Deprecated `amount_sol` param is still accepted as UI units."""
        raw, _decimals, ui, err = await _resolve_amount_raw(
            {"amount_sol": 0.1},
            SOL_MINT,
            portfolio,
            "wallet",
        )
        assert err is None
        assert ui == 0.1
        assert raw == 100_000_000


class TestExecuteSwapPercentSizing:
    """End-to-end test: percent_of_balance with SQUIRE sell."""

    @pytest.fixture()
    def mock_dex(self) -> JupiterDex:
        dex = MagicMock(spec=JupiterDex)
        dex.get_quote = AsyncMock(
            return_value=SwapQuote(
                input_mint=SQUIRE_MINT,
                output_mint=SOL_MINT,
                in_amount=250_000_000_000,  # 250k SQUIRE raw
                out_amount=30_000_000,  # 0.03 SOL
                price_impact_pct=0.5,
                slippage_bps=50,
                raw={},
            )
        )
        dex.execute_swap = AsyncMock(
            return_value=TradeExecution(
                success=True,
                signature="sell_sig",
                input_mint=SQUIRE_MINT,
                output_mint=SOL_MINT,
                in_amount=250_000_000_000,
                out_amount=30_000_000,
                actual_out_amount=29_900_000,
                gas_lamports=5000,
            )
        )
        dex.get_token_price = AsyncMock(return_value=0.0003)
        dex.check_feasibility = AsyncMock(
            return_value=FeasibilityResult(feasible=True, price_impact_pct=0.5, reason="OK")
        )
        return dex

    @pytest.fixture()
    def portfolio(self) -> Portfolio:
        p = MagicMock(spec=Portfolio)
        p.get_sol_balance = AsyncMock(return_value=0.05)
        # Wallet holds 500,000 SQUIRE
        p.get_token_balance = AsyncMock(return_value=500_000.0)
        return p

    @pytest.fixture()
    def registry(
        self,
        sample_config: Config,
        mock_dex: JupiterDex,
        portfolio: Portfolio,
        tmp_path: Path,
    ) -> ToolRegistry:
        _DECIMALS_CACHE[SQUIRE_MINT] = 6
        reg = ToolRegistry()
        ledger = TradeLedger(storage_dir=str(tmp_path))
        register_tools(
            reg,
            config=sample_config,
            jupiter_dex=mock_dex,
            portfolio=portfolio,
            wallet_address="wallet",
            ledger=ledger,
            session_id="test",
        )
        kp = Keypair()
        reg._set_trading_keypair(kp)
        return reg

    async def test_sell_50_percent_of_balance(
        self, registry: ToolRegistry, mock_dex: JupiterDex
    ) -> None:
        result = json.loads(
            await registry.execute(
                "execute_swap",
                {
                    "input_mint": SQUIRE_MINT,
                    "output_mint": SOL_MINT,
                    "percent_of_balance": 50.0,
                },
            )
        )
        assert result["success"] is True

        # Verify the amount passed to Jupiter was 250,000 * 1e6 = 2.5e11
        mock_dex.execute_swap.assert_called_once()
        args = mock_dex.execute_swap.call_args.args
        # args: (keypair, input_mint, output_mint, amount_raw, slippage)
        assert args[1] == SQUIRE_MINT
        assert args[2] == SOL_MINT
        assert args[3] == 250_000_000_000  # 250k SQUIRE in raw atoms
