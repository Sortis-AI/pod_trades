"""End-to-end smoke test: import, construct, run one turn."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pod_the_trader.trading.dex import JupiterDex
from pod_the_trader.trading.portfolio import Portfolio
from pod_the_trader.trading.transaction import TransactionBuilder


class TestSmoke:
    def test_package_imports(self) -> None:
        """Verify the package imports without errors."""

    def test_config_with_valid_address(self, sample_config) -> None:
        assert sample_config.get("agent.name") == "Pod The Trader"
        assert sample_config.get("agent.model") == "minimax-m2.7"
        assert sample_config.get("trading.target_token_address") is not None

    def test_all_tools_registered(self, sample_config) -> None:
        from pod_the_trader.tools import create_registry

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

        tool_names = registry.tool_names
        assert len(tool_names) >= 16
        assert "get_solana_balance" in tool_names
        assert "execute_swap" in tool_names
        assert "calculate_pnl" in tool_names

    async def test_agent_run_turn(self, sample_config, tmp_path: Path) -> None:
        from pod_the_trader.agent.core import TradingAgent
        from pod_the_trader.agent.memory import ConversationMemory
        from pod_the_trader.level5.client import Level5Client
        from pod_the_trader.tools.registry import ToolRegistry

        registry = ToolRegistry()

        async def price_handler(args: dict) -> dict:
            return {"price_usd": 150.0}

        registry.register(
            "get_market_price",
            "Get price",
            {"type": "object", "properties": {"mint_address": {"type": "string"}}},
            price_handler,
        )

        memory = ConversationMemory(storage_dir=str(tmp_path))

        mock_level5 = MagicMock(spec=Level5Client)
        mock_level5.is_registered.return_value = True
        mock_level5.get_api_base_url.return_value = "https://proxy"
        mock_level5._api_token = "test_token"

        msg = MagicMock()
        msg.content = "The current price is $150."
        msg.tool_calls = None
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"
        response = MagicMock()
        response.choices = [choice]

        with patch("pod_the_trader.agent.core.AsyncOpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=response)
            mock_openai_cls.return_value = mock_client

            agent = TradingAgent(sample_config, mock_level5, registry, memory)
            result = await agent.run_turn("What is the current price?")

        assert isinstance(result, str)
        assert "150" in result
