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
    with patch("pod_the_trader.agent.core._PodTraderAsyncOpenAI"):
        return TradingAgent(sample_config, mock_level5, registry, memory)


class TestConstruction:
    def test_raises_without_level5(self, sample_config: Config, registry, memory) -> None:
        mock_l5 = MagicMock(spec=Level5Client)
        mock_l5.is_registered.return_value = False
        with pytest.raises(ValueError, match="Level5 registration required"):
            TradingAgent(sample_config, mock_l5, registry, memory)

    def test_openai_client_constructed_with_5_retries(
        self,
        sample_config: Config,
        mock_level5: Level5Client,
        registry,
        memory,
    ) -> None:
        # Regression test for the 0.3.3 fix: the OpenAI SDK defaults
        # max_retries to 2, which wasn't enough to ride out the
        # transient "internal: cache error" 500s the proxy throws under
        # cache pressure. Bumped to 5 to match the Level5/Jupiter
        # client retry policy and the user's "1-minute outage budget".
        with patch("pod_the_trader.agent.core._PodTraderAsyncOpenAI") as mock_openai:
            TradingAgent(sample_config, mock_level5, registry, memory)
        assert mock_openai.called
        kwargs = mock_openai.call_args.kwargs
        assert kwargs.get("max_retries") == 5


class TestPodTraderBackoffSchedule:
    """The OpenAI SDK's default ``INITIAL_RETRY_DELAY=0.5``,
    ``MAX_RETRY_DELAY=8`` produces a ~15s total backoff for 5 retries
    — out of step with the 62s budget Level5/Jupiter give the
    upstream. ``_PodTraderAsyncOpenAI`` overrides the SDK's
    ``_calculate_retry_timeout`` so all three core API surfaces share
    the same 2/4/8/16/32 schedule.
    """

    def test_backoff_schedule_matches_2_4_8_16_32(self) -> None:
        from pod_the_trader.agent.core import _pod_trader_backoff_seconds

        # Attempt 0 is the first retry (after the first failure).
        assert _pod_trader_backoff_seconds(0) == 2.0
        assert _pod_trader_backoff_seconds(1) == 4.0
        assert _pod_trader_backoff_seconds(2) == 8.0
        assert _pod_trader_backoff_seconds(3) == 16.0
        assert _pod_trader_backoff_seconds(4) == 32.0
        # 5 retries (= the configured max_retries) sum to 62 seconds
        # of wall-clock budget. That's the "1-minute outage" the user
        # specified for unified retry policy.
        cumulative = sum(_pod_trader_backoff_seconds(i) for i in range(5))
        assert cumulative == 62.0

    def test_override_uses_custom_schedule(
        self,
        sample_config: Config,
        mock_level5: Level5Client,
        registry,
        memory,
    ) -> None:
        # Build a real subclass instance and exercise the override.
        # Skip the network: the constructor doesn't actually talk to
        # OpenAI, it just stores config.
        from pod_the_trader.agent.core import _PodTraderAsyncOpenAI

        client = _PodTraderAsyncOpenAI(
            base_url="https://api.level5.cloud/proxy/x/v1",
            api_key="level5",
            max_retries=5,
        )

        # Stub `options.get_max_retries(self.max_retries)` to return 5.
        class _StubOptions:
            def get_max_retries(self, default: int) -> int:
                return 5

        # No Retry-After header → custom schedule.
        timeout_first = client._calculate_retry_timeout(
            remaining_retries=5,
            options=_StubOptions(),
            response_headers=None,
        )
        assert timeout_first == 2.0
        timeout_last = client._calculate_retry_timeout(
            remaining_retries=1,
            options=_StubOptions(),
            response_headers=None,
        )
        assert timeout_last == 32.0

    def test_override_honors_retry_after_header(
        self,
        sample_config: Config,
        mock_level5: Level5Client,
        registry,
        memory,
    ) -> None:
        # Within the reasonable range (1-60s) the SDK's policy is to
        # use whatever the server asks. We preserve that — a Cloudflare
        # 429 with Retry-After is the server telling us about rate
        # limits, and ignoring it is impolite + counterproductive.
        import httpx as _httpx

        from pod_the_trader.agent.core import _PodTraderAsyncOpenAI

        client = _PodTraderAsyncOpenAI(
            base_url="https://api.level5.cloud/proxy/x/v1",
            api_key="level5",
            max_retries=5,
        )

        class _StubOptions:
            def get_max_retries(self, default: int) -> int:
                return 5

        headers = _httpx.Headers({"retry-after": "15"})
        timeout = client._calculate_retry_timeout(
            remaining_retries=5,
            options=_StubOptions(),
            response_headers=headers,
        )
        assert timeout == 15.0


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


class TestPlanWithoutActionGuard:
    """When the model emits text describing tool calls but never
    actually invokes any tool (the plan-without-action failure mode
    seen against minimax-m2.7), run_turn must nudge once inline and
    give the model a chance to either act or commit to a DECISION.
    The guard is bounded — at most one extra LLM call per turn — so a
    persistently misbehaving model can't loop us forever.
    """

    async def test_plan_text_without_tool_calls_triggers_nudge(self, agent: TradingAgent) -> None:
        # First response: a plan-table-style answer with no tool_calls
        # and no DECISION line — the exact pathology from production.
        plan_resp = _make_response(
            content=(
                "**Gathering fresh market data:**\n"
                "| step | action |\n"
                "| 1 | get_market_price |\n"
                "| 2 | get_price_history |"
            )
        )
        # Second response: model complies with the nudge by emitting a
        # DECISION line.
        decision_resp = _make_response(
            content="DECISION: HOLD — data unavailable, no entry signal."
        )
        agent._client.chat.completions.create = AsyncMock(side_effect=[plan_resp, decision_resp])

        result = await agent.run_turn("Cycle prompt")

        # Both responses concatenated; one extra LLM call beyond the
        # initial one because the guard fired exactly once.
        assert "DECISION: HOLD" in result
        assert agent._client.chat.completions.create.call_count == 2
        # The nudge user message should have been added to memory
        # between the two LLM calls.
        second_call_messages = agent._client.chat.completions.create.call_args_list[1].kwargs[
            "messages"
        ]
        assert any(
            m.get("role") == "user" and "tool_calls" in (m.get("content") or "")
            for m in second_call_messages
        )

    async def test_plan_guard_fires_at_most_once(self, agent: TradingAgent) -> None:
        # A model that keeps emitting plan-text with no tools and no
        # DECISION must not trap us in a nudge loop. The guard fires
        # exactly once per run_turn and then lets the loop exit with
        # whatever text accumulated — the downstream
        # _enforce_decision_format will reprompt at the trade_loop
        # level if needed.
        bad_resp = _make_response(content="I will call X, Y, Z next.")
        agent._client.chat.completions.create = AsyncMock(return_value=bad_resp)

        result = await agent.run_turn("Cycle prompt")

        assert "I will call X" in result
        # Exactly two calls: the initial one plus the single nudge.
        assert agent._client.chat.completions.create.call_count == 2

    async def test_guard_skipped_when_tool_was_called(self, agent: TradingAgent) -> None:
        # If the model successfully called any tool during the turn,
        # plan-without-action by definition didn't happen — the guard
        # must not fire even if the final response lacks a DECISION.
        tc = _make_tool_call("call_1", "test_tool", '{"x": "hi"}')
        tool_resp = _make_response(tool_calls=[tc], finish_reason="tool_calls")
        final_resp = _make_response(content="Tool ran, but no decision yet.")
        agent._client.chat.completions.create = AsyncMock(side_effect=[tool_resp, final_resp])

        await agent.run_turn("Use a tool")

        # Exactly two calls: the tool-call round-trip. The guard saw
        # tool_calls_made > 0 and stayed silent.
        assert agent._client.chat.completions.create.call_count == 2


class TestTradeContextRefresh:
    """Trade context (the ``Cost-basis ledger ...`` line in the system
    prompt) must be recomputed at the top of each cycle. Without that,
    a FIFO close that shifts the open-lot composition is invisible to
    the model — it reasons against avg cost frozen at bootstrap. This
    is the bug 0.3.2 fixes.
    """

    def _make_agent_with_ledger(
        self,
        tmp_path,
        sample_config: Config,
        mock_level5: Level5Client,
        registry: ToolRegistry,
        memory: ConversationMemory,
    ) -> TradingAgent:
        from pod_the_trader.data.lot_ledger import LotLedger
        from pod_the_trader.data.price_log import PriceLog

        lot_ledger = LotLedger(storage_dir=str(tmp_path))
        price_log = PriceLog(storage_dir=str(tmp_path))
        with patch("pod_the_trader.agent.core._PodTraderAsyncOpenAI"):
            return TradingAgent(
                sample_config,
                mock_level5,
                registry,
                memory,
                lot_ledger=lot_ledger,
                price_log=price_log,
            )

    def _append_lot(
        self,
        ledger,
        kind: str,
        qty: float,
        price: float,
        target_mint: str,
        ts: str = "2026-05-14T00:00:00+00:00",
    ) -> None:
        from pod_the_trader.data.lot_ledger import LotEvent

        ledger.append(
            LotEvent(
                timestamp=ts,
                mint=target_mint,
                kind=kind,
                qty=qty,
                unit_price_usd=price,
                source="trade",
            )
        )

    def _append_price_tick(self, price_log, mint: str, price: float) -> None:
        from pod_the_trader.data.price_log import PriceTick, now_iso

        price_log.append(
            PriceTick(
                timestamp=now_iso(),
                mint=mint,
                symbol="",
                price_usd=price,
                source="test",
            )
        )

    def test_refresh_updates_avg_cost_after_fifo_close(
        self,
        tmp_path,
        sample_config: Config,
        mock_level5: Level5Client,
        registry: ToolRegistry,
        memory: ConversationMemory,
    ) -> None:
        agent = self._make_agent_with_ledger(tmp_path, sample_config, mock_level5, registry, memory)
        target = sample_config.get("trading.target_token_address")

        # Two open lots — one cheap, one expensive. Weighted blend
        # produces an avg cost between them.
        self._append_lot(agent._lot_ledger, "open", 1000.0, 0.0001, target)
        self._append_lot(agent._lot_ledger, "open", 1000.0, 0.0005, target)
        self._append_price_tick(agent._price_log, target, 0.0006)

        agent._refresh_trade_context_from_price_log()
        ctx_before = agent._memory.get_trade_context()
        assert "avg cost $0.00030000" in ctx_before  # (0.0001 + 0.0005) / 2

        # Close the cheap lot — FIFO eats the older one first. Avg
        # cost of remaining open lot should jump to $0.0005.
        self._append_lot(agent._lot_ledger, "close", 1000.0, 0.0007, target)
        agent._refresh_trade_context_from_price_log()
        ctx_after = agent._memory.get_trade_context()
        assert "avg cost $0.00050000" in ctx_after
        assert ctx_before != ctx_after

    def test_refresh_handles_empty_ledger_gracefully(
        self,
        tmp_path,
        sample_config: Config,
        mock_level5: Level5Client,
        registry: ToolRegistry,
        memory: ConversationMemory,
    ) -> None:
        # No lots, no price ticks → no trade_context set, no crash.
        agent = self._make_agent_with_ledger(tmp_path, sample_config, mock_level5, registry, memory)
        agent._refresh_trade_context_from_price_log()
        assert agent._memory.get_trade_context() == ""

    def test_refresh_uses_latest_price_tick(
        self,
        tmp_path,
        sample_config: Config,
        mock_level5: Level5Client,
        registry: ToolRegistry,
        memory: ConversationMemory,
    ) -> None:
        agent = self._make_agent_with_ledger(tmp_path, sample_config, mock_level5, registry, memory)
        target = sample_config.get("trading.target_token_address")
        self._append_lot(agent._lot_ledger, "open", 1000.0, 0.001, target)

        # Two ticks; the latest one is what should price the unrealized PnL.
        self._append_price_tick(agent._price_log, target, 0.002)
        self._append_price_tick(agent._price_log, target, 0.005)

        agent._refresh_trade_context_from_price_log()
        ctx = agent._memory.get_trade_context()
        # Unrealized PnL = 1000 × (0.005 − 0.001) = $4.0000 (formatted .4f)
        assert "unrealized PnL $4.0000" in ctx


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
        # The plan-without-action guard fires when a response carries no
        # DECISION and no tool_calls (a benign "OK" qualifies), so
        # run_turn nudges once and concatenates both responses. We
        # don't care which form the result takes — just that the call
        # didn't crash and the model's content propagated through.
        assert "OK" in result


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
