from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .domain import ToolCallRecord, ToolResult, utcnow


ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class DiagnosticTool:
    name: str
    schema: dict[str, type]
    handler: ToolHandler
    timeout_seconds: float = 5.0
    max_output_chars: int = 8000
    description: str = ""


class ToolRegistry:
    def __init__(self, tools: list[DiagnosticTool]):
        self._tools = {tool.name: tool for tool in tools}

    def descriptions(self) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "arguments": {k: v.__name__ for k, v in t.schema.items()}} for t in self._tools.values()]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolCallRecord:
        started_at = utcnow()
        started = time.perf_counter()
        tool = self._tools.get(name)
        if tool is None:
            result = ToolResult.error("agent", "unknown_tool", f"Tool is not allowed: {name}")
        else:
            invalid = [key for key, typ in tool.schema.items() if key not in arguments or not isinstance(arguments[key], typ)]
            extra = sorted(set(arguments) - set(tool.schema))
            if invalid or extra:
                result = ToolResult.error("agent", "invalid_arguments", f"invalid={invalid}, extra={extra}")
            else:
                try:
                    result = await asyncio.wait_for(tool.handler(arguments), timeout=tool.timeout_seconds)
                except asyncio.TimeoutError:
                    result = ToolResult.error(tool.name, "timeout", "tool timed out", retryable=True)
                except Exception as exc:
                    result = ToolResult.error(tool.name, type(exc).__name__, str(exc)[:500], retryable=True)
                rendered = str(result.data)
                if len(rendered) > tool.max_output_chars:
                    result.data = {"truncated": True, "preview": rendered[:tool.max_output_chars]}
        return ToolCallRecord(name, dict(arguments), result, started_at, int((time.perf_counter() - started) * 1000))
