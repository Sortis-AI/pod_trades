"""Integration test: full trade cycle with mocked externals."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pod_the_trader.agent.core import TradingAgent
from pod_the_trader.agent.memory import ConversationMemory
from pod_the_trader.config import Config
from pod_the_trader.level5.client import Level5Client
from pod_the_trader.tools import create_registry
from pod_the_trader.trading.dex import JupiterDex, SwapQuote
from pod_the_trader.trading.portfolio import Portfolio
from pod_the_trader.trading.transaction import TransactionBuilder


def _make_tool_call(call_id: str, name: str, arguments: str) -> MagicMock:
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def _make_response(content=None, tool_calls=None, finish_reason="stop"):
    resp = MagicMock()
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp.choices = [choice]
    return resp


class TestTradeCycle:
    async def test_full_cycle(self, sample_config: Config, tmp_path: Path) -> None:
        mock_dex = MagicMock(spec=JupiterDex)
        mock_dex.get_token_price = AsyncMock(return_value=0.15)
        mock_dex.get_quote = AsyncMock(
            return_value=SwapQuote(
                input_mint="SOL",
                output_mint="TOKEN",
                in_amount=100_000_000,
                out_amount=50_000,
                price_impact_pct=0.3,
                slippage_bps=50,
                raw={},
            )
        )

        mock_portfolio = Portfolio(
            rpc_url="https://api.devnet.solana.com",
            jupiter_dex=mock_dex,
            storage_dir=str(tmp_path),
        )

        mock_tx = MagicMock(spec=TransactionBuilder)
        mock_level5 = MagicMock(spec=Level5Client)
        mock_level5.is_registered.return_value = True
        mock_level5.get_api_base_url.return_value = "https://proxy"
        mock_level5._api_token = "test_token"
        mock_level5.get_balance = AsyncMock(return_value=10.0)

        registry = create_registry(
            config=sample_config,
            portfolio=mock_portfolio,
            jupiter_dex=mock_dex,
            transaction_builder=mock_tx,
            rpc_url="https://api.devnet.solana.com",
            wallet_address="11111111111111111111111111111111",
        )

        memory = ConversationMemory(storage_dir=str(tmp_path))

        # 1. Agent asks for quote
        tc = _make_tool_call(
            "call_1",
            "get_swap_quote",
            '{"input_mint": "So11111111111111111111111111111111111111112", '
            '"output_mint": "So11111111111111111111111111111111111111112", '
            '"amount_sol": 0.1}',
        )
        resp1 = _make_response(tool_calls=[tc], finish_reason="tool_calls")

        # 2. Agent responds with analysis
        resp2 = _make_response(content="Based on the quote, I recommend waiting.")

        with patch("pod_the_trader.agent.core.AsyncOpenAI") as mock_openai_cls:
            mock_openai = MagicMock()
            mock_openai.chat.completions.create = AsyncMock(side_effect=[resp1, resp2])
            mock_openai_cls.return_value = mock_openai

            agent = TradingAgent(sample_config, mock_level5, registry, memory)
            result = await agent.run_turn("Analyze market")

        assert "recommend" in result.lower()
        assert mock_openai.chat.completions.create.call_count == 2
