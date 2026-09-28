import asyncio

from diagnostics.domain import ToolResult
from diagnostics.runbooks import KeywordRunbook
from diagnostics.tools import DiagnosticTool, ToolRegistry


def test_tool_distinguishes_timeout_from_not_found():
    async def slow(_):
        await asyncio.sleep(0.05)
        return ToolResult.not_found("exchange_rest")

    record = asyncio.run(ToolRegistry([
        DiagnosticTool("query", {"id": str}, slow, timeout_seconds=0.001)
    ]).call("query", {"id": "1"}))
    assert record.result.status == "error"
    assert record.result.error_type == "timeout"
    assert record.result.retryable is True


def test_tool_rejects_extra_and_wrong_typed_arguments():
    async def handler(_):
        return ToolResult.ok("local", {})

    registry = ToolRegistry([DiagnosticTool("query", {"id": str}, handler)])
    result = asyncio.run(registry.call("query", {"id": 1, "sql": "drop table"}))
    assert result.result.error_type == "invalid_arguments"


def test_keyword_runbook_returns_source_version_and_applicability(tmp_path):
    path = tmp_path / "runbook.md"
    path.write_text("# OKX 51000\nApplies to order queries. Retry REST before escalation.", encoding="utf-8")
    result = KeywordRunbook([path], version="2026-09-28").search("51000 order", limit=2)
    assert result.status == "ok"
    assert result.data["items"][0]["source"].endswith("runbook.md")
    assert result.data["items"][0]["version"] == "2026-09-28"
    assert "applicability" in result.data["items"][0]
