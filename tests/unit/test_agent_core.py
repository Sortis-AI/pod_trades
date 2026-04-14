"""Tests for pod_the_trader.agent.core."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pod_the_trader.agent.core import TradingAgent
from pod_the_trader.agent.memory import ConversationMemory
from pod_the_trader.config import Config
from pod_the_trader.level5.client import Level5Client
from pod_the_trader.tools.registry import ToolRegistry


def _make_tool_call(call_id: str, name: str, arguments: str) -> MagicMock:
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def _make_response(
    content: str | None = None,
    tool_calls: list | None = None,
    finish_reason: str = "stop",
) -> MagicMock:
    resp = MagicMock()
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp.choices = [choice]
    return resp


@pytest.fixture()
def mock_level5() -> Level5Client:
    client = MagicMock(spec=Level5Client)
    client.is_registered.return_value = True
    client.get_api_base_url.return_value = "https://api.level5.cloud/v1/tok/proxy"
    client._api_token = "test_token"
    client.get_balance = AsyncMock(return_value=10.0)
    return client


@pytest.fixture()
def registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def echo_handler(args: dict) -> dict:
        return {"echoed": args}

    reg.register(
        "test_tool",
        "A test tool",
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        echo_handler,
    )
    return reg


@pytest.fixture()
def memory(tmp_path) -> ConversationMemory:
    return ConversationMemory(storage_dir=str(tmp_path))


@pytest.fixture()
def agent(
    sample_config: Config,
    mock_level5: Level5Client,
    registry: ToolRegistry,
    memory: ConversationMemory,
) -> TradingAgent:
    with patch("pod_the_trader.agent.core.AsyncOpenAI"):
        return TradingAgent(sample_config, mock_level5, registry, memory)


class TestConstruction:
    def test_raises_without_level5(self, sample_config: Config, registry, memory) -> None:
        mock_l5 = MagicMock(spec=Level5Client)
        mock_l5.is_registered.return_value = False
        with pytest.raises(ValueError, match="Level5 registration required"):
            TradingAgent(sample_config, mock_l5, registry, memory)


class TestSystemPrompt:
    def test_system_prompt_contains_target(self, agent: TradingAgent) -> None:
        prompt = agent._build_system_prompt()
        assert "So11111111111111111111111111111111111111112" in prompt

    def test_system_prompt_includes_trade_context(self, agent: TradingAgent) -> None:
        agent._memory.set_trade_context("Last trade: bought 100 tokens")
        prompt = agent._build_system_prompt()
        assert "bought 100 tokens" in prompt


class TestRunTurn:
    async def test_text_only_response(self, agent: TradingAgent) -> None:
        text_resp = _make_response(content="The price is $150")
        agent._client.chat.completions.create = AsyncMock(return_value=text_resp)

        result = await agent.run_turn("What is the price?")
        assert "150" in result

    async def test_system_message_in_messages(self, agent: TradingAgent) -> None:
        text_resp = _make_response(content="OK")
        agent._client.chat.completions.create = AsyncMock(return_value=text_resp)

        await agent.run_turn("Hello")

        call_kwargs = agent._client.chat.completions.create.call_args.kwargs
        messages = call_kwargs["messages"]
        assert messages[0]["role"] == "system"

    async def test_tool_use_triggers_execution(self, agent: TradingAgent) -> None:
        tc = _make_tool_call("call_1", "test_tool", '{"x": "hello"}')
        tool_resp = _make_response(tool_calls=[tc], finish_reason="tool_calls")
        final_resp = _make_response(content="Done with tool")
        agent._client.chat.completions.create = AsyncMock(side_effect=[tool_resp, final_resp])

        result = await agent.run_turn("Use the tool")
        assert "Done with tool" in result
        assert agent._client.chat.completions.create.call_count == 2

    async def test_tool_result_sent_back(self, agent: TradingAgent) -> None:
        tc = _make_tool_call("call_1", "test_tool", '{"x": "test"}')
        tool_resp = _make_response(tool_calls=[tc], finish_reason="tool_calls")
        final_resp = _make_response(content="Result processed")
        agent._client.chat.completions.create = AsyncMock(side_effect=[tool_resp, final_resp])

        await agent.run_turn("Test")

        # The second call should have tool result messages
        second_call = agent._client.chat.completions.create.call_args_list[1]
        messages = second_call.kwargs["messages"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"

    async def test_multi_tool_response(self, agent: TradingAgent) -> None:
        tc1 = _make_tool_call("call_1", "test_tool", '{"x": "a"}')
        tc2 = _make_tool_call("call_2", "test_tool", '{"x": "b"}')
        multi_resp = _make_response(tool_calls=[tc1, tc2], finish_reason="tool_calls")
        final_resp = _make_response(content="Both done")
        agent._client.chat.completions.create = AsyncMock(side_effect=[multi_resp, final_resp])

        result = await agent.run_turn("Use both tools")
        assert "Both done" in result

    async def test_tool_loop_max_iterations(self, agent: TradingAgent) -> None:
        tc = _make_tool_call("call_x", "test_tool", '{"x": "loop"}')
        tool_resp = _make_response(tool_calls=[tc], finish_reason="tool_calls")
        agent._client.chat.completions.create = AsyncMock(return_value=tool_resp)

        await agent.run_turn("Loop forever")
        max_iter = agent._config.get("agent.max_iterations_per_turn", 10)
        assert agent._client.chat.completions.create.call_count <= max_iter + 1

    async def test_invalid_json_arguments(self, agent: TradingAgent) -> None:
        tc = _make_tool_call("call_1", "test_tool", "not-json")
        tool_resp = _make_response(tool_calls=[tc], finish_reason="tool_calls")
        final_resp = _make_response(content="Done")
        agent._client.chat.completions.create = AsyncMock(side_effect=[tool_resp, final_resp])

        result = await agent.run_turn("Bad args")
        assert "Done" in result

    async def test_no_response_text(self, agent: TradingAgent) -> None:
        resp = _make_response(content=None)
        agent._client.chat.completions.create = AsyncMock(return_value=resp)
        result = await agent.run_turn("Hello")
        assert result == "No response generated."


class TestTradeTracking:
    async def test_trade_count_increments_on_swap(self, agent: TradingAgent) -> None:
        async def swap_handler(args: dict) -> dict:
            return {"success": True, "signature": "sig123"}

        agent._registry.register(
            "execute_swap",
            "Swap",
            {"type": "object", "properties": {}},
            swap_handler,
        )

        tc = _make_tool_call("call_1", "execute_swap", "{}")
        tool_resp = _make_response(tool_calls=[tc], finish_reason="tool_calls")
        final_resp = _make_response(content="Swap done")
        agent._client.chat.completions.create = AsyncMock(side_effect=[tool_resp, final_resp])

        assert agent.trade_count == 0
        await agent.run_turn("Execute a swap")
        assert agent.trade_count == 1
        assert agent.last_trade_time is not None


class TestTradeLoop:
    async def test_trade_loop_respects_shutdown(self, agent: TradingAgent) -> None:
        shutdown = asyncio.Event()
        shutdown.set()  # Immediate shutdown
        await agent.trade_loop(shutdown)
        # Should exit immediately without error

    async def test_trade_loop_low_balance_pauses(self, agent: TradingAgent) -> None:
        agent._level5.get_balance = AsyncMock(return_value=0.5)
        shutdown = asyncio.Event()

        async def stop_after_one():
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.create_task(stop_after_one())
        await agent.trade_loop(shutdown)

    async def test_trade_loop_handles_balance_error(self, agent: TradingAgent) -> None:
        agent._level5.get_balance = AsyncMock(side_effect=Exception("network"))
        resp = _make_response(content="Analysis done")
        agent._client.chat.completions.create = AsyncMock(return_value=resp)

        shutdown = asyncio.Event()

        async def stop_after_one():
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.create_task(stop_after_one())
        await agent.trade_loop(shutdown)

    async def test_trade_loop_handles_turn_error(self, agent: TradingAgent) -> None:
        agent._client.chat.completions.create = AsyncMock(side_effect=Exception("API error"))

        shutdown = asyncio.Event()

        async def stop_after_one():
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.create_task(stop_after_one())
        await agent.trade_loop(shutdown)


class TestLowBalance:
    async def test_low_balance_does_not_crash(self, agent: TradingAgent) -> None:
        agent._level5.get_balance = AsyncMock(return_value=0.5)
        text_resp = _make_response(content="OK")
        agent._client.chat.completions.create = AsyncMock(return_value=text_resp)

        result = await agent.run_turn("Check status")
        assert result == "OK"


class TestDecisionExecutionEnforcement:
    """If the model writes DECISION: SELL/BUY but never calls execute_swap,
    ``_enforce_decision_execution`` must nudge the model once more and then
    (if the model still doesn't comply) append a system-override HOLD line
    so the displayed decision matches what actually happened.
    """

    async def test_unexecuted_sell_is_downgraded_to_hold(self, agent: TradingAgent) -> None:
        # The nudge call goes through run_turn, which hits the LLM client.
        # Stub it so we control the follow-up response without touching the
        # trade loop at all. The follow-up still claims SELL → override fires.
        resp2 = _make_response(content="DECISION: SELL — I really mean it this time.")
        agent._client.chat.completions.create = AsyncMock(return_value=resp2)

        trade_count_before = agent.trade_count  # 0
        response = "DECISION: SELL — Time to take profit."
        result = await agent._enforce_decision_execution(response, trade_count_before)

        assert "system override" in result
        assert "DECISION: HOLD" in result
        # Trade count unchanged (no execute_swap ever fired)
        assert agent.trade_count == trade_count_before

    async def test_unexecuted_sell_can_be_rescued_by_followup_hold(
        self, agent: TradingAgent
    ) -> None:
        # Follow-up response correctly downgrades to HOLD on its own — no
        # override needed.
        resp2 = _make_response(content="DECISION: HOLD — On reflection, staying put.")
        agent._client.chat.completions.create = AsyncMock(return_value=resp2)

        result = await agent._enforce_decision_execution(
            "DECISION: SELL — Time to take profit.", agent.trade_count
        )
        assert "system override" not in result
        # Parser still sees the final DECISION: HOLD from the follow-up
        assert "DECISION: HOLD" in result

    async def test_executed_sell_is_not_enforced(self, agent: TradingAgent) -> None:
        # Simulate that trade_count increased during run_turn (as it would
        # if execute_swap had actually been called). The enforcement path
        # should become a no-op and the response returned unchanged.
        trade_count_before = agent.trade_count
        agent._trade_count = trade_count_before + 1

        original = "DECISION: SELL — Took profit."
        result = await agent._enforce_decision_execution(original, trade_count_before)
        assert result == original

    async def test_hold_decision_is_not_enforced(self, agent: TradingAgent) -> None:
        # HOLD doesn't require a swap, so enforcement is a no-op even if
        # trade_count is unchanged.
        result = await agent._enforce_decision_execution(
            "DECISION: HOLD — No signal.", agent.trade_count
        )
        assert "system override" not in result
        assert result == "DECISION: HOLD — No signal."

    async def test_unknown_decision_format_gets_nudged(self, agent: TradingAgent) -> None:
        # Separate helper: _enforce_decision_format sends a nudge if the
        # response has no parseable DECISION line.
        resp2 = _make_response(content="DECISION: HOLD — Stable.")
        agent._client.chat.completions.create = AsyncMock(return_value=resp2)

        result = await agent._enforce_decision_format("Some rambling analysis.")
        assert "DECISION: HOLD" in result
