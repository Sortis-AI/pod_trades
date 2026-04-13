"""Tests for pod_the_trader.agent.memory."""

import json
from pathlib import Path

import pytest

from pod_the_trader.agent.memory import ConversationMemory


@pytest.fixture()
def memory(tmp_path: Path) -> ConversationMemory:
    return ConversationMemory(storage_dir=str(tmp_path), max_messages=10)


class TestMessageManagement:
    def test_add_and_get_messages(self, memory: ConversationMemory) -> None:
        memory.add_message("user", "Hello")
        memory.add_message("assistant", "Hi there")
        msgs = memory.get_messages()
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"
        assert msgs[1]["role"] == "assistant"

    def test_get_messages_limit(self, memory: ConversationMemory) -> None:
        for i in range(10):
            memory.add_message("user", f"Message {i}")
        msgs = memory.get_messages(limit=3)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "Message 7"

    def test_clear(self, memory: ConversationMemory) -> None:
        memory.add_message("user", "Test")
        memory.clear()
        assert memory.message_count == 0
        assert memory.get_messages() == []


class TestPersistence:
    def test_save_and_load(self, tmp_path: Path) -> None:
        mem1 = ConversationMemory(storage_dir=str(tmp_path))
        mem1.add_message("user", "Saved message")
        mem1.set_trade_context("Recent trade: bought 100 tokens")
        mem1.save()

        mem2 = ConversationMemory(storage_dir=str(tmp_path))
        mem2.load()
        msgs = mem2.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Saved message"
        assert mem2.get_trade_context() == "Recent trade: bought 100 tokens"

    def test_load_nonexistent_is_safe(self, tmp_path: Path) -> None:
        mem = ConversationMemory(storage_dir=str(tmp_path / "nonexistent"))
        mem.load()
        assert mem.message_count == 0


class TestSummarization:
    def test_summarizes_when_over_limit(self, memory: ConversationMemory) -> None:
        for i in range(15):
            memory.add_message("user", f"Message {i}")

        assert memory.message_count == 15
        memory.summarize()
        # Should have been condensed: 1 summary + max_messages//2 recent
        assert memory.message_count <= memory._max_messages

    def test_no_summarize_when_under_limit(self, memory: ConversationMemory) -> None:
        for i in range(5):
            memory.add_message("user", f"Message {i}")

        memory.summarize()
        assert memory.message_count == 5

    def test_summary_is_first_message(self, memory: ConversationMemory) -> None:
        for i in range(15):
            memory.add_message("user", f"Message {i}")

        memory.summarize()
        msgs = memory.get_messages(limit=100)
        assert "summary" in msgs[0]["content"].lower()


class TestContentBlocks:
    def test_add_dict_content_blocks(self, memory: ConversationMemory) -> None:
        blocks = [
            {"type": "tool_result", "tool_use_id": "abc", "content": "result"},
        ]
        memory.add_message("user", blocks)
        msgs = memory.get_messages()
        assert msgs[0]["content"] == blocks

    def test_add_string_content(self, memory: ConversationMemory) -> None:
        memory.add_message("assistant", "Plain text")
        msgs = memory.get_messages()
        assert msgs[0]["content"] == "Plain text"


class TestTradeContext:
    def test_set_and_get(self, memory: ConversationMemory) -> None:
        memory.set_trade_context("Latest: bought 50 tokens at $0.15")
        assert "bought 50 tokens" in memory.get_trade_context()


class TestStripToolMessages:
    def test_removes_tool_results(self, memory: ConversationMemory) -> None:
        memory.add_message("user", "hi")
        memory.add_message(
            "assistant",
            {
                "role": "assistant",
                "content": "thinking",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ],
            },
        )
        memory.add_message("tool", {"role": "tool", "tool_call_id": "c1", "content": "r"})
        memory.add_message("assistant", "final answer")

        memory.strip_tool_messages()
        msgs = memory.get_messages(limit=100)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "hi"
        # Assistant with tool_calls should be kept as text-only
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "thinking"
        assert "tool_calls" not in msgs[1]
        assert msgs[2]["content"] == "final answer"

    def test_drops_assistant_with_no_content(self, memory: ConversationMemory) -> None:
        memory.add_message("user", "hi")
        memory.add_message(
            "assistant",
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ],
            },
        )
        memory.add_message("tool", {"role": "tool", "tool_call_id": "c1", "content": "r"})
        memory.strip_tool_messages()
        msgs = memory.get_messages(limit=100)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hi"

    def test_plain_messages_unchanged(self, memory: ConversationMemory) -> None:
        memory.add_message("user", "a")
        memory.add_message("assistant", "b")
        memory.add_message("user", "c")
        memory.strip_tool_messages()
        msgs = memory.get_messages(limit=100)
        assert len(msgs) == 3


class TestToolChainValidation:
    def _write_state(self, tmp_path: Path, messages: list) -> Path:
        storage = tmp_path / ".pod_the_trader"
        storage.mkdir(parents=True, exist_ok=True)
        state_file = storage / "conversation.json"
        state_file.write_text(json.dumps({"messages": messages, "trade_context": ""}))
        return storage

    def _make_memory(self, storage_dir: Path) -> ConversationMemory:
        return ConversationMemory(storage_dir=str(storage_dir))

    def test_complete_tool_chain_stripped_to_text_only(self, tmp_path: Path) -> None:
        # Load always strips tool messages — keeps only plain text turns.
        messages = [
            {"role": "user", "content": "analyze"},
            {
                "role": "assistant",
                "content": "thinking",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "t1", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "t2", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
            {"role": "tool", "tool_call_id": "call_2", "content": "r2"},
            {"role": "assistant", "content": "done"},
        ]
        storage = self._write_state(tmp_path, messages)
        memory = self._make_memory(storage)
        memory.load()
        loaded = memory.get_messages(limit=100)
        # user, assistant "thinking" (text-only), assistant "done"
        assert len(loaded) == 3
        assert all("tool_calls" not in m for m in loaded)
        assert all(m.get("role") != "tool" for m in loaded)

    def test_incomplete_tool_chain_truncated(self, tmp_path: Path) -> None:
        # Crash scenario: assistant made 2 tool calls but only 1 result
        # was saved before shutdown.
        messages = [
            {"role": "user", "content": "first prompt"},
            {"role": "assistant", "content": "first response"},
            {"role": "user", "content": "second prompt"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "t1", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "t2", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
            # missing: tool call_2 result
        ]
        storage = self._write_state(tmp_path, messages)
        memory = self._make_memory(storage)
        memory.load()
        loaded = memory.get_messages(limit=100)
        # Everything before the broken assistant should be kept
        assert len(loaded) == 3
        assert loaded[-1]["content"] == "second prompt"

    def test_missing_all_tool_results_truncated(self, tmp_path: Path) -> None:
        messages = [
            {"role": "user", "content": "prompt"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "t1", "arguments": "{}"},
                    },
                ],
            },
            # no tool results at all
        ]
        storage = self._write_state(tmp_path, messages)
        memory = self._make_memory(storage)
        memory.load()
        loaded = memory.get_messages(limit=100)
        assert len(loaded) == 1
        assert loaded[0]["content"] == "prompt"

    def test_multiple_complete_chains(self, tmp_path: Path) -> None:
        messages = [
            {"role": "user", "content": "q1"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "r"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "b", "content": "r"},
            {"role": "assistant", "content": "done"},
        ]
        storage = self._write_state(tmp_path, messages)
        memory = self._make_memory(storage)
        memory.load()
        loaded = memory.get_messages(limit=100)
        # After strip: user "q1", assistant "done"
        # (both assistant-with-tool-calls messages had no content, so dropped)
        assert len(loaded) == 2
        assert loaded[0]["content"] == "q1"
        assert loaded[1]["content"] == "done"

    def test_no_tool_calls_unchanged(self, tmp_path: Path) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        storage = self._write_state(tmp_path, messages)
        memory = self._make_memory(storage)
        memory.load()
        assert len(memory.get_messages(limit=100)) == 2
