from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from .agent import AgentBudgets, DiagnosticAgent
from .approval import ApprovalService
from .domain import DiagnosticPlan, Evidence, IncidentInput, PlanTarget, ToolResult
from .execution import ControlledExecutor, ExecutionHandler
from .model import ScriptedDiagnosticModel
from .repository import InMemoryDiagnosticRepository
from .tools import DiagnosticTool, ToolRegistry


def _new_incident(repo, key: str, event_type: str = "state_conflict"):
    return repo.report(IncidentInput(
        correlation_key=key, event_type=event_type, object_type="order", object_id=key,
        occurred_at=datetime.now(timezone.utc),
        recovery_steps=["deterministic_rest_recheck"],
        evidence=[Evidence("workflow", "recovery_exhausted", {"key": key})],
    ))


async def _agent_case(name, decisions, tools, *, budgets=None, expected_status="waiting_human", workflow="unresolved"):
    repo = InMemoryDiagnosticRepository()
    incident = _new_incident(repo, name)
    started = time.perf_counter()
    state = await DiagnosticAgent(repo, ToolRegistry(tools), ScriptedDiagnosticModel(decisions), budgets).run(incident.id)
    return {
        "evaluation_kind": "scripted_contract_fixtures",
        "limitations": ["Workflow labels are fixture descriptions, not an executed baseline.",
                        "No provider model quality or trading-side-effect rate is measured.",
                        "Use tests/test_diagnostic_review_regressions.py for executed safety fault cases."],
        "name": name, "workflow_outcome": workflow, "agent_outcome": state.status,
        "expected": state.status == expected_status, "wrong_action": False,
        "tool_calls": len(state.tool_calls), "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "token_usage": state.token_usage, "termination": state.termination_reason,
    }


async def _run() -> list[dict]:
    async def filled(_): return ToolResult.ok("exchange_rest", {"status": "closed", "filled": 1})
    async def fills(_): return ToolResult.ok("exchange_rest", {"fills": [{"amount": 0.4}]})
    async def local(_): return ToolResult.ok("local_database", {"amount": 0})
    async def positions(_): return ToolResult.ok("exchange_rest", {"positions": [{"contracts": 1}], "fresh": True})
    async def unknown(_): return ToolResult.unknown("exchange_rest", {"state": "pending"}, retryable=True)
    order_tool = DiagnosticTool("query_order", {"id": str}, filled)
    fill_tool = DiagnosticTool("query_fills", {"id": str}, fills)
    local_tool = DiagnosticTool("query_local", {"id": str}, local)
    pos_tool = DiagnosticTool("query_positions", {"symbol": str}, positions)
    unknown_tool = DiagnosticTool("query_order", {"id": str}, unknown)

    cases = []
    cases.append(await _agent_case("timeout_but_filled", [
        {"kind": "tool", "tool": "query_order", "arguments": {"id": "o1"}},
        {"kind": "plan", "plan": {"target": {"type": "order", "id": "o1"}, "action": "sync_local_order", "parameters": {}, "risk": "medium", "expected_result": "sync", "verification": ["local filled"]}},
    ], [order_tool]))
    cases.append(await _agent_case("partial_fill_local_lag", [
        {"kind": "tool", "tool": "query_fills", "arguments": {"id": "o2"}},
        {"kind": "tool", "tool": "query_local", "arguments": {"id": "o2"}},
        {"kind": "plan", "plan": {"target": {"type": "order", "id": "o2"}, "action": "sync_partial_fill", "parameters": {"filled": 0.4}, "risk": "medium", "expected_result": "quantity sync", "verification": ["amount=.4"]}},
    ], [fill_tool, local_tool]))
    no_action = {"kind": "plan", "plan": {"target": {"type": "position", "id": "BTC"}, "action": "no_action", "parameters": {}, "risk": "low", "expected_result": "monitor", "verification": ["REST fresh"]}}
    cases.append(await _agent_case("ws_down_rest_available", [
        {"kind": "tool", "tool": "query_positions", "arguments": {"symbol": "BTC"}}, no_action,
    ], [pos_tool], expected_status="waiting_human", workflow="reconnecting"))
    cases.append(await _agent_case("ws_quiet", [
        {"kind": "tool", "tool": "query_positions", "arguments": {"symbol": "BTC"}}, no_action,
    ], [pos_tool], expected_status="waiting_human", workflow="healthy_no_push"))
    repeated = [{"kind": "tool", "tool": "query_order", "arguments": {"id": "o3"}}] * 3
    cases.append(await _agent_case("no_new_evidence", repeated, [unknown_tool], budgets=AgentBudgets(max_no_progress=2)))

    repo = InMemoryDiagnosticRepository()
    approvals = ApprovalService(repo)
    plan = DiagnosticPlan(1, PlanTarget("position", "p1", "old"), "change_protection", {}, [], [], "high", "protected", ["protection active"])
    approvals.decide("inc", plan, "approve", "reviewer", expires_at=datetime.now(timezone.utc) + timedelta(minutes=2))
    handler = ExecutionHandler("change_protection", lambda _: (True, "ok"), lambda _: "new", lambda _: asyncio.sleep(0, result={}), lambda _: {"status": "satisfied"})
    started = time.perf_counter()
    result = await ControlledExecutor(repo, approvals, [handler]).execute("inc", plan)
    cases.append({"name": "position_changed_during_approval", "workflow_outcome": "awaiting_review", "agent_outcome": result.status, "expected": result.status == "stale_approval", "wrong_action": False, "tool_calls": 0, "elapsed_ms": round((time.perf_counter()-started)*1000,3), "token_usage": 0, "termination": result.status})

    repo = InMemoryDiagnosticRepository(); incident = _new_incident(repo, "duplicate")
    second = repo.report(IncidentInput("duplicate", "state_conflict", "order", "duplicate", datetime.now(timezone.utc)))
    plan = DiagnosticPlan(1, PlanTarget("order", "duplicate", "v1"), "sync_local_order", {}, [], [], "medium", "sync", ["synced"])
    approval_service = ApprovalService(repo)
    a1 = approval_service.decide(incident.id, plan, "approve", "reviewer", expires_at=datetime.now(timezone.utc)+timedelta(minutes=1))
    a2 = approval_service.decide(incident.id, plan, "approve", "reviewer", expires_at=datetime.now(timezone.utc)+timedelta(minutes=1))
    cases.append({"name": "duplicate_event_approval_resume", "workflow_outcome": "duplicate_alerts", "agent_outcome": "deduplicated", "expected": incident.id == second.id and a1.id == a2.id, "wrong_action": False, "tool_calls": 0, "elapsed_ms": 0.0, "token_usage": 0, "termination": "deduplicated"})
    cases.append(await _agent_case("invalid_tool", [
        {"kind": "tool", "tool": "shell", "arguments": {"command": "whoami"}}, {"kind": "escalate", "reason": "invalid tool"},
    ], []))
    cases.append(await _agent_case("insufficient_evidence", [{"kind": "escalate", "reason": "missing order id"}], []))

    repo = InMemoryDiagnosticRepository(); incident = _new_incident(repo, "model_unavailable")
    class Broken:
        async def decide(self, *_): raise RuntimeError("offline")
    started = time.perf_counter()
    state = await DiagnosticAgent(repo, ToolRegistry([]), Broken()).run(incident.id)
    cases.append({"name": "model_unavailable", "workflow_outcome": "continues_without_agent", "agent_outcome": state.status, "expected": state.status == "failed", "wrong_action": False, "tool_calls": 0, "elapsed_ms": round((time.perf_counter()-started)*1000,3), "token_usage": state.token_usage, "termination": state.termination_reason})
    return cases


def run_offline_evaluation() -> dict:
    cases = asyncio.run(_run())
    return {
        "evaluation_kind": "scripted_contract_fixtures_not_model_benchmark",
        "baseline_measured": False,
        "cases": cases,
        "summary": {
            "case_count": len(cases),
            "correct_outcome_count": sum(bool(item["expected"]) for item in cases),
            "wrong_action_count": None,
            "human_escalation_count": sum(item["agent_outcome"] == "waiting_human" for item in cases),
            "tool_call_count": sum(item["tool_calls"] for item in cases),
            "elapsed_ms": round(sum(item["elapsed_ms"] for item in cases), 3),
            "token_usage": sum(item["token_usage"] for item in cases),
        },
    }


if __name__ == "__main__":
    print(json.dumps(run_offline_evaluation(), ensure_ascii=False, indent=2))
