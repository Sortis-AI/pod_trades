"""Conversation state management and persistence."""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConversationMemory:
    """Manages conversation message history with persistence and summarization."""

    def __init__(
        self,
        storage_dir: str = "~/.pod_the_trader",
        max_messages: int = 30,
    ) -> None:
        self._storage_dir = Path(storage_dir).expanduser()
        self._storage_path = self._storage_dir / "conversation.json"
        self._max_messages = max_messages
        self._messages: list[dict[str, Any]] = []
        self._trade_context: str = ""

    def add_message(self, role: str, content: Any) -> None:
        """Add a message to history.

        Content can be a string, or a dict representing a full message
        (for tool results or assistant messages with tool_calls).
        """
        if isinstance(content, str):
            self._messages.append({"role": role, "content": content})
        elif isinstance(content, dict):
            # Full message dict (tool result, assistant with tool_calls)
            self._messages.append(content)
        else:
            # Fallback: serialize SDK objects
            serialized = self._serialize_content(content)
            self._messages.append({"role": role, "content": serialized})

    def get_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the last N messages."""
        return self._messages[-limit:]

    def clear(self) -> None:
        """Clear all messages."""
        self._messages.clear()

    def strip_tool_messages(self) -> None:
        """Remove tool call / tool result messages from history.

        Keeps only plain user and assistant text messages. This is called at
        the end of each cycle because some LLM providers (minimax via
        OpenRouter) don't accept tool_call_ids from past turns — they reject
        requests that replay stale IDs with "tool id not found" errors.
        """
        cleaned: list[dict[str, Any]] = []
        for msg in self._messages:
            role = msg.get("role")
            if role == "tool":
                continue
            if role == "assistant" and msg.get("tool_calls"):
                # Keep the assistant text content if any, drop tool_calls
                content = msg.get("content")
                if content:
                    cleaned.append({"role": "assistant", "content": content})
                continue
            cleaned.append(msg)
        self._messages = cleaned

    def save(self) -> None:
        """Persist conversation state to disk."""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "messages": self._messages,
            "trade_context": self._trade_context,
        }
        self._storage_path.write_text(json.dumps(data, default=str))
        logger.debug("Saved conversation state (%d messages)", len(self._messages))

    def load(self) -> None:
        """Load conversation state from disk.

        Validates tool call chains on load: truncates history before any
        assistant message whose tool_calls are not all followed by matching
        tool result messages. Incomplete chains (from a crashed cycle) would
        cause the LLM provider to reject the next request.
        """
        if not self._storage_path.is_file():
            return
        try:
            data = json.loads(self._storage_path.read_text())
            raw_messages = data.get("messages", [])
            self._messages = self._validate_tool_chains(raw_messages)
            # Strip tool messages on load too — stale tool_call_ids from a
            # prior session will be rejected by minimax.
            self.strip_tool_messages()
            self._trade_context = data.get("trade_context", "")
            logger.debug(
                "Loaded conversation state (%d raw, %d after cleanup)",
                len(raw_messages),
                len(self._messages),
            )
        except Exception as e:
            logger.warning("Failed to load conversation state: %s", e)

    @staticmethod
    def _validate_tool_chains(messages: list[dict]) -> list[dict]:
        """Return a truncated message list with only complete tool call chains.

        Walks messages in order. When it encounters an assistant message with
        tool_calls, it checks that every tool_call_id has a matching "tool"
        result message following it (before the next non-tool message). If
        not, the history is truncated before the incomplete assistant message.
        """
        result: list[dict] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_call_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
                # Look ahead for matching tool results
                j = i + 1
                seen_ids: set[str] = set()
                while j < len(messages) and messages[j].get("role") == "tool":
                    seen_ids.add(messages[j].get("tool_call_id"))
                    j += 1
                if not tool_call_ids.issubset(seen_ids):
                    # Incomplete chain — stop before this assistant message
                    logger.warning(
                        "Truncating conversation at incomplete tool chain (missing %d results)",
                        len(tool_call_ids - seen_ids),
                    )
                    break
                # Chain is complete — keep the assistant + tool messages
                result.append(msg)
                result.extend(messages[i + 1 : j])
                i = j
            else:
                result.append(msg)
                i += 1
        return result

    def summarize(self) -> None:
        """Condense older messages when history exceeds max_messages.

        Keeps the most recent messages and replaces older ones with a summary.
        """
        if len(self._messages) <= self._max_messages:
            return

        keep_count = self._max_messages // 2
        old_messages = self._messages[:-keep_count]
        recent_messages = self._messages[-keep_count:]

        summary_parts = []
        for msg in old_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                summary_parts.append(f"[{role}] {content[:200]}")

        summary_text = "Previous conversation summary:\n" + "\n".join(summary_parts[-10:])

        self._messages = [
            {"role": "user", "content": summary_text},
            *recent_messages,
        ]
        logger.info(
            "Summarized conversation: %d old messages -> 1 summary + %d recent",
            len(old_messages),
            len(recent_messages),
        )

    def set_trade_context(self, context: str) -> None:
        """Update the running trade context for system prompt injection."""
        self._trade_context = context

    def get_trade_context(self) -> str:
        """Return the current trade context string."""
        return self._trade_context

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def _serialize_content(self, content: Any) -> Any:
        """Serialize Anthropic SDK content blocks to JSON-safe dicts."""
        if isinstance(content, list):
            result = []
            for block in content:
                if isinstance(block, dict):
                    result.append(block)
                elif hasattr(block, "model_dump"):
                    result.append(block.model_dump())
                elif hasattr(block, "__dict__"):
                    result.append({"type": getattr(block, "type", "unknown"), **block.__dict__})
                else:
                    result.append(str(block))
            return result
        if hasattr(content, "model_dump"):
            return content.model_dump()
        return content
