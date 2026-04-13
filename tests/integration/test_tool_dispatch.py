"""Integration test: tool dispatch round-trip."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pod_the_trader.agent.core import TradingAgent
from pod_the_trader.agent.memory import ConversationMemory
from pod_the_trader.config import Config
from pod_the_trader.level5.client import Level5Client
from pod_the_trader.tools.registry import ToolRegistry


class TestToolDispatch:
    async def test_tool_called_and_result_sent(self, sample_config: Config, tmp_path: Path) -> None:
        registry = ToolRegistry()
        call_log: list[dict] = []

        async def custom_handler(args: dict) -> dict:
            call_log.append(args)
            return {"answer": 42}

        registry.register(
            "custom_tool",
            "A custom test tool",
            {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
            custom_handler,
        )

        memory = ConversationMemory(storage_dir=str(tmp_path))
        mock_level5 = MagicMock(spec=Level5Client)
        mock_level5.is_registered.return_value = True
        mock_level5.get_api_base_url.return_value = "https://proxy"
        mock_level5._api_token = "test_token"

        # Response 1: tool call
        tc = MagicMock()
        tc.id = "tool_call_1"
        tc.function.name = "custom_tool"
        tc.function.arguments = '{"question": "What is the answer?"}'

        msg1 = MagicMock()
        msg1.content = None
        msg1.tool_calls = [tc]
        choice1 = MagicMock()
        choice1.message = msg1
        choice1.finish_reason = "tool_calls"
        resp1 = MagicMock()
        resp1.choices = [choice1]

        # Response 2: final text
        msg2 = MagicMock()
        msg2.content = "The answer is 42."
        msg2.tool_calls = None
        choice2 = MagicMock()
        choice2.message = msg2
        choice2.finish_reason = "stop"
        resp2 = MagicMock()
        resp2.choices = [choice2]

        with patch("pod_the_trader.agent.core.AsyncOpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(side_effect=[resp1, resp2])
            mock_openai_cls.return_value = mock_client

            agent = TradingAgent(sample_config, mock_level5, registry, memory)
            result = await agent.run_turn("Ask the custom tool")

        # Tool was called with correct args
        assert len(call_log) == 1
        assert call_log[0]["question"] == "What is the answer?"

        # Verify tool result was sent in the second API call
        second_call = mock_client.chat.completions.create.call_args_list[1]
        messages = second_call.kwargs["messages"]

        found_tool_result = False
        for msg in messages:
            if msg.get("role") == "tool":
                assert msg["tool_call_id"] == "tool_call_1"
                result_data = json.loads(msg["content"])
                assert result_data["answer"] == 42
                found_tool_result = True

        assert found_tool_result, "tool result not found in follow-up call"
        assert result == "The answer is 42."
