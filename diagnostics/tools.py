from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .domain import ToolCallRecord, ToolResult, utcnow
from .redaction import redact

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

    def register(self, tool: DiagnosticTool) -> None:
        if tool.name in self._tools:
            raise ValueError("tool already registered")
        self._tools[tool.name] = tool

    def descriptions(self) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "arguments": {k: v.__name__ for k, v in t.schema.items()}} for t in self._tools.values()]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolCallRecord:
        started_at = utcnow()
        started = time.perf_counter()
        tool = self._tools.get(name)
        if not isinstance(arguments, dict):
            result = ToolResult.error("agent", "invalid_arguments", "arguments must be an object")
            arguments = {}
        elif tool is None:
            result = ToolResult.error("agent", "unknown_tool", f"Tool is not allowed: {name}")
        else:
            invalid = [key for key, typ in tool.schema.items() if key not in arguments or type(arguments[key]) is not typ]
            invalid += [key for key, value in arguments.items() if
                        (isinstance(value, str) and len(value) > 512) or
                        (key == "limit" and type(value) is int and not 1 <= value <= 100)]
            extra = sorted(set(arguments) - set(tool.schema))
            if invalid or extra:
                result = ToolResult.error("agent", "invalid_arguments", f"invalid={invalid}, extra={extra}")
            else:
                try:
                    result = await asyncio.wait_for(tool.handler(arguments), timeout=tool.timeout_seconds)
                except asyncio.TimeoutError:
                    result = ToolResult.error(tool.name, "timeout", "tool timed out", retryable=True)
                except Exception as exc:  # noqa: BLE001 - untrusted adapter boundary
                    result = ToolResult.error(tool.name, type(exc).__name__, str(exc)[:500], retryable=True)
                result.data = redact(result.data)
                result.message = redact(result.message)
                rendered = str(result.data)
                if len(rendered) > tool.max_output_chars:
                    result.truncated = True
                    result.data = {"truncated": True, "preview": rendered[:tool.max_output_chars]}
        return ToolCallRecord(name, dict(arguments), result, started_at, int((time.perf_counter() - started) * 1000))
