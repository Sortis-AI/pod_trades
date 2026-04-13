"""Integration test: startup flow wiring."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from pod_the_trader.config import Config
from pod_the_trader.tools import create_registry
from pod_the_trader.trading.dex import JupiterDex
from pod_the_trader.trading.portfolio import Portfolio
from pod_the_trader.trading.transaction import TransactionBuilder

EXPECTED_TOOLS = [
    "get_solana_balance",
    "get_spl_token_balance",
    "get_token_info",
    "get_recent_transactions",
    "get_market_price",
    "get_token_details",
    "analyze_market_conditions",
    "get_target_token_status",
    "get_swap_quote",
    "execute_swap",
    "check_swap_feasibility",
    "get_token_price",
    "get_portfolio_overview",
    "get_token_balance",
    "get_trade_history",
    "calculate_pnl",
]


class TestStartupWiring:
    def test_all_tools_registered(self, sample_config: Config, tmp_path: Path) -> None:
        mock_dex = MagicMock(spec=JupiterDex)
        mock_dex.get_token_price = AsyncMock(return_value=150.0)

        mock_portfolio = MagicMock(spec=Portfolio)
        mock_tx = MagicMock(spec=TransactionBuilder)

        registry = create_registry(
            config=sample_config,
            portfolio=mock_portfolio,
            jupiter_dex=mock_dex,
            transaction_builder=mock_tx,
            rpc_url="https://api.devnet.solana.com",
            wallet_address="11111111111111111111111111111111",
        )

        registered = registry.tool_names
        for tool_name in EXPECTED_TOOLS:
            assert tool_name in registered, f"Missing tool: {tool_name}"

    def test_tool_definitions_are_openai_format(
        self, sample_config: Config, tmp_path: Path
    ) -> None:
        mock_dex = MagicMock(spec=JupiterDex)
        mock_portfolio = MagicMock(spec=Portfolio)
        mock_tx = MagicMock(spec=TransactionBuilder)

        registry = create_registry(
            config=sample_config,
            portfolio=mock_portfolio,
            jupiter_dex=mock_dex,
            transaction_builder=mock_tx,
            rpc_url="https://api.devnet.solana.com",
            wallet_address="11111111111111111111111111111111",
        )

        for defn in registry.get_all_definitions():
            assert defn["type"] == "function", "Missing type=function"
            assert "function" in defn
            fn = defn["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn
