import asyncio
from datetime import datetime, timezone

from diagnostics.agent import AgentBudgets, DiagnosticAgent
from diagnostics.domain import Evidence, IncidentInput, ToolResult
from diagnostics.model import ScriptedDiagnosticModel
from diagnostics.repository import InMemoryDiagnosticRepository
from diagnostics.tools import DiagnosticTool, ToolRegistry


def incident(repo):
    return repo.report(IncidentInput(
        correlation_key="order:abc",
        event_type="order_result_unknown",
        object_type="order",
        object_id="abc",
        occurred_at=datetime.now(timezone.utc),
        evidence=[Evidence("workflow", "timeout", {"message": "request timed out"})],
    ))


def test_tool_feedback_changes_next_investigation_step():
    repo = InMemoryDiagnosticRepository()
    item = incident(repo)
    calls = []

    async def run_tool(args):
        calls.append(args["order_id"])
        return ToolResult.ok("exchange_rest", {"state": "filled"})

    registry = ToolRegistry([DiagnosticTool("query_order", {"order_id": str}, run_tool)])
    model = ScriptedDiagnosticModel([
        {"kind": "tool", "tool": "query_order", "arguments": {"order_id": "abc"}},
        {"kind": "plan", "candidate_causes": [{"cause": "response_lost", "because": "REST says filled"}],
         "plan": {"target": {"type": "order", "id": "abc"}, "action": "sync_local_order",
                  "parameters": {"state": "filled"}, "evidence_ids": ["latest"],
                  "preconditions": ["order remains filled"], "risk": "medium",
                  "expected_result": "local order matches exchange", "verification": ["local state is filled"]}},
    ])
    state = asyncio.run(DiagnosticAgent(repo, registry, model).run(item.id))

    assert calls == ["abc"]
    assert state.status == "waiting_human"
    assert state.tool_calls[0].result.status == "ok"
    assert state.plan.action == "sync_local_order"


def test_equivalent_results_stop_no_progress_loop():
    repo = InMemoryDiagnosticRepository()
    item = incident(repo)

    async def unchanged(_):
        return ToolResult.unknown("exchange_rest", {"state": "pending"}, retryable=True)

    registry = ToolRegistry([DiagnosticTool("query_order", {"order_id": str}, unchanged)])
    model = ScriptedDiagnosticModel([
        {"kind": "tool", "tool": "query_order", "arguments": {"order_id": "abc"}},
        {"kind": "tool", "tool": "query_order", "arguments": {"order_id": "abc"}},
        {"kind": "tool", "tool": "query_order", "arguments": {"order_id": "abc"}},
    ])
    state = asyncio.run(DiagnosticAgent(
        repo, registry, model,
        AgentBudgets(max_steps=7, max_tool_calls=7, max_no_progress=2),
    ).run(item.id))

    assert state.status == "waiting_human"
    assert state.termination_reason == "no_new_evidence"
    assert len(state.tool_calls) == 2


def test_invalid_tool_arguments_are_data_not_permissions():
    repo = InMemoryDiagnosticRepository()
    item = incident(repo)
    registry = ToolRegistry([])
    model = ScriptedDiagnosticModel([
        {"kind": "tool", "tool": "shell", "arguments": {"command": "rm"}},
        {"kind": "escalate", "reason": "insufficient evidence"},
    ])
    state = asyncio.run(DiagnosticAgent(repo, registry, model).run(item.id))

    assert state.status == "waiting_human"
    assert state.tool_calls[0].result.error_type == "unknown_tool"


def test_model_failure_terminates_without_running_tools():
    repo = InMemoryDiagnosticRepository()
    item = incident(repo)

    class BrokenModel:
        async def decide(self, _state, _tools):
            raise RuntimeError("model unavailable")

    state = asyncio.run(DiagnosticAgent(repo, ToolRegistry([]), BrokenModel()).run(item.id))
    assert state.status == "failed"
    assert state.tool_calls == []


def test_agent_resumes_persisted_step_after_restart():
    repo = InMemoryDiagnosticRepository()
    item = incident(repo)
    repo.save_checkpoint(item.id, {
        "incident_id": item.id, "status": "investigating", "step": 2,
        "started_at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": 1,
        "token_usage": 12, "candidate_causes": [], "pending_information": [],
        "plan": None, "termination_reason": None, "tool_calls": [],
    })
    model = ScriptedDiagnosticModel([{"kind": "escalate", "reason": "resume complete"}])
    state = asyncio.run(DiagnosticAgent(repo, ToolRegistry([]), model).run(item.id))
    assert state.step == 3
    assert state.token_usage > 12
