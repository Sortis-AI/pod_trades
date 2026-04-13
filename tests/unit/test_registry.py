"""Tests for pod_the_trader.tools.registry."""

import json

import pytest

from pod_the_trader.tools.registry import ToolRegistry


@pytest.fixture()
def registry() -> ToolRegistry:
    return ToolRegistry()


async def _echo_handler(args: dict) -> dict:
    return {"echoed": args}


async def _failing_handler(args: dict) -> dict:
    raise ValueError("intentional failure")


class TestRegister:
    def test_stores_tool(self, registry: ToolRegistry) -> None:
        schema = {"type": "object", "properties": {}}
        registry.register("test_tool", "A test", schema, _echo_handler)
        assert registry.get_tool("test_tool") is not None
        assert registry.get_tool("test_tool").name == "test_tool"

    def test_get_tool_returns_none_for_unknown(self, registry: ToolRegistry) -> None:
        assert registry.get_tool("nonexistent") is None


class TestDefinitions:
    def test_openai_format(self, registry: ToolRegistry) -> None:
        registry.register(
            "my_tool",
            "Does something",
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            _echo_handler,
        )
        defs = registry.get_all_definitions()
        assert len(defs) == 1
        d = defs[0]

        # Must have OpenAI format
        assert d["type"] == "function"
        assert "function" in d
        assert d["function"]["name"] == "my_tool"
        assert d["function"]["description"] == "Does something"
        assert d["function"]["parameters"]["required"] == ["x"]

    def test_multiple_tools(self, registry: ToolRegistry) -> None:
        for i in range(3):
            schema = {"type": "object", "properties": {}}
            registry.register(f"tool_{i}", f"Tool {i}", schema, _echo_handler)
        defs = registry.get_all_definitions()
        assert len(defs) == 3
        names = {d["function"]["name"] for d in defs}
        assert names == {"tool_0", "tool_1", "tool_2"}


class TestExecute:
    async def test_calls_handler_returns_json(self, registry: ToolRegistry) -> None:
        schema = {"type": "object", "properties": {}}
        registry.register("echo", "Echo", schema, _echo_handler)
        result = await registry.execute("echo", {"msg": "hello"})
        parsed = json.loads(result)
        assert parsed == {"echoed": {"msg": "hello"}}

    async def test_catches_handler_exceptions(self, registry: ToolRegistry) -> None:
        schema = {"type": "object", "properties": {}}
        registry.register("fail", "Fail", schema, _failing_handler)
        result = await registry.execute("fail", {})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "intentional failure" in parsed["error"]

    async def test_unknown_tool_returns_error(self, registry: ToolRegistry) -> None:
        result = await registry.execute("nope", {})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Unknown tool" in parsed["error"]


class TestInstanceIsolation:
    def test_registries_are_independent(self) -> None:
        r1 = ToolRegistry()
        r2 = ToolRegistry()
        schema = {"type": "object", "properties": {}}
        r1.register("only_in_r1", "Test", schema, _echo_handler)
        assert r1.get_tool("only_in_r1") is not None
        assert r2.get_tool("only_in_r1") is None

    def test_tool_names_property(self) -> None:
        r = ToolRegistry()
        schema = {"type": "object", "properties": {}}
        r.register("a", "A", schema, _echo_handler)
        r.register("b", "B", schema, _echo_handler)
        assert set(r.tool_names) == {"a", "b"}
