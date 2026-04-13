"""Tool registry with OpenAI-format definitions."""

import json
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


@dataclass
class Tool:
    """A registered tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    """Registry of callable tools for the LLM agent.

    Emits tool definitions in OpenAI function-calling format.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        """Register a tool by name."""
        self._tools[name] = Tool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
        )
        logger.debug("Registered tool: %s", name)

    def get_tool(self, name: str) -> Tool | None:
        """Look up a tool by name. Returns None if not found."""
        return self._tools.get(name)

    def get_all_definitions(self) -> list[dict[str, Any]]:
        """Return all tool definitions in OpenAI function-calling format.

        Each definition has: type="function", function={name, description, parameters}.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        """Execute a tool by name. Returns a JSON string."""
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"Unknown tool: {name}"})

        try:
            result = await tool.handler(args)
            return json.dumps(result)
        except Exception as e:
            logger.error("Tool '%s' failed: %s", name, e)
            return json.dumps({"error": f"Tool execution failed: {e}"})

    @property
    def tool_names(self) -> list[str]:
        """List registered tool names."""
        return list(self._tools.keys())
