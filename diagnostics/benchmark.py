"""Executed fake-exchange comparison with observable action counters."""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from .approval import ApprovalService
from .domain import DiagnosticPlan, IncidentInput, PlanTarget, utcnow
from .execution import ControlledExecutor
from .remediation import recovery_handler
from .sandbox import Sandbox
from .service import IncidentService

SCENARIOS = (
    "timeout_filled", "partial_fill", "ws_disconnected", "ws_quiet", "no_evidence",
    "position_changed", "duplicate_event", "duplicate_approval", "restart",
    "invalid_tool", "invalid_arguments", "insufficient_evidence", "model_unavailable",
    "restart_worker", "action_timeout",
)


async def workflow(sandbox):
    """Conservative reference baseline: bounded order check and transport repair."""
    data = sandbox.read()
    if data["scenario"] in ("ws_disconnected", "ws_quiet"):
        data["ws_connected"] = True
        data["snapshot_fresh"] = True
        sandbox.write(data)
        sandbox.repository.set_status(sandbox.incident().id, "resolved")
        return "resolved"
    await sandbox.tools().call("query_order", {"order_id": data["order_id"]})
    sandbox.repository.set_status(sandbox.incident().id, "waiting_human")
    return "waiting_human"


async def run_case(directory, scenario):
    baseline = Sandbox(directory / f"{scenario}-baseline.db")
    baseline.initialize(scenario)
    baseline_start = time.perf_counter()
    baseline_status = await workflow(baseline)
    baseline_latency = time.perf_counter() - baseline_start
    sandbox = Sandbox(directory / f"{scenario}-agent.db")
    sandbox.initialize(scenario)
    started = time.perf_counter()
    state = None
    execution_status = None
    process_ok = True
    if scenario in ("ws_disconnected", "ws_quiet"):
        status = await workflow(sandbox)
    elif scenario == "restart_worker":
        plan = DiagnosticPlan(1, PlanTarget("worker", "ws", "generation-1"), "restart_websocket_worker",
                              {}, [], ["worker failed"], "L2", "healthy", ["health and reconciliation"])
        approval = ApprovalService(sandbox.repository)
        approval.decide(sandbox.incident().id, plan, "approve", "evaluation-operator")
        async def recover():
            data = sandbox.read()
            data["worker_alive"] = True
            data["actions"].append({"action": plan.action, "parameters": {}})
            sandbox.write(data)
        async def health():
            return {"alive": sandbox.read()["worker_alive"], "authenticated": True,
                    "subscribed": True, "snapshot_fresh": True, "exit_healthy": True}
        async def reconcile():
            return sandbox.read()["worker_alive"]
        handler = recovery_handler(plan.action, recover=recover, fingerprint=lambda _: "generation-1",
                                   safe=lambda _: (not sandbox.read()["worker_alive"], "only failed worker"),
                                   health=health, reconcile=reconcile)
        result = await ControlledExecutor(sandbox.repository, approval, [handler]).execute(sandbox.incident().id, plan)
        execution_status = result.status
        status = sandbox.incident().status
    else:
        if scenario == "duplicate_event":
            service = IncidentService(sandbox.repository, enabled=True)
            item = sandbox.incident()
            value = IncidentInput(item.correlation_key, item.event_type, item.object_type, item.object_id, utcnow())
            service.report_nowait(value)
            service.report_nowait(value)
            process_ok = service.queue.qsize() == 1 and len(sandbox.repository.list_incidents()) == 1
        if scenario == "restart":
            sandbox.engine.dispose()
            sandbox = Sandbox(directory / f"{scenario}-agent.db")
        state = await sandbox.investigate()
        if state.plan:
            first = sandbox.review("approve", "evaluation-operator")
            if scenario == "duplicate_approval":
                process_ok = first.id == sandbox.review("approve", "evaluation-operator").id
            if scenario == "position_changed":
                data = sandbox.read()
                data["revision"] += 1
                sandbox.write(data)
            if scenario == "restart":
                sandbox.engine.dispose()
                sandbox = Sandbox(directory / f"{scenario}-agent.db")
            result = await sandbox.execute()
            if scenario in ("duplicate_approval", "action_timeout", "restart"):
                result = await sandbox.execute()
            execution_status = result.status
        status = sandbox.incident().status
    data = sandbox.read()
    actions = data["actions"]
    readonly = scenario in ("position_changed", "invalid_tool", "invalid_arguments", "insufficient_evidence",
                           "model_unavailable", "no_evidence", "ws_disconnected", "ws_quiet")
    wrong = sum(readonly or (action["action"] == "sync_local_order" and
                             action["parameters"] != {"filled": data["filled"]}) for action in actions)
    duplicates = max(0, len(actions) - 1)
    if scenario == "position_changed":
        business_ok = execution_status == "stale_approval" and not actions
    elif scenario in ("invalid_tool", "invalid_arguments", "insufficient_evidence", "no_evidence"):
        business_ok = status == "waiting_human" and not actions
    elif scenario == "model_unavailable":
        business_ok = status == "failed" and not actions
    elif scenario in ("ws_disconnected", "ws_quiet"):
        business_ok = status == "resolved" and data["ws_connected"] and not actions and state is None
    elif scenario == "restart_worker":
        business_ok = status == "resolved" and data["worker_alive"]
    else:
        business_ok = status == "resolved" and data["local_filled"] == data["filled"] and len(actions) == 1
    calls = state.tool_calls if state else []
    if scenario == "partial_fill":
        process_ok = process_ok and any(call.tool == "query_fills" for call in calls)
    if scenario in ("invalid_tool", "invalid_arguments"):
        process_ok = process_ok and bool(calls) and calls[0].result.error_type == ("unknown_tool" if scenario == "invalid_tool" else "invalid_arguments")
    baseline_queries = len(baseline.read()["queries"])
    baseline.engine.dispose()
    sandbox.engine.dispose()
    return {
        "name": scenario, "incident_count": 1, "status": status, "execution_status": execution_status,
        "business_state_correct": bool(business_ok), "tool_policy_correct": bool(process_ok),
        "passed": bool(business_ok and process_ok and not wrong and not duplicates),
        "wrong_action_count": wrong, "duplicate_action_count": duplicates, "action_count": len(actions),
        "tool_calls": len(calls), "loop_steps": state.step if state else 0,
        "latency_seconds": round(time.perf_counter() - started, 6),
        "model_tokens": None, "estimated_decision_tokens": state.token_usage if state else 0,
        "baseline": {"status": baseline_status, "tool_calls": baseline_queries,
                     "latency_seconds": round(baseline_latency, 6)},
    }


async def _run(directory):
    return [await run_case(directory, scenario) for scenario in SCENARIOS]


def evaluate():
    with tempfile.TemporaryDirectory(prefix="tradepilot-eval-") as temporary:
        cases = asyncio.run(_run(Path(temporary)))
    return {"kind": "fake_exchange_executed_comparison", "n": len(cases), "cases": cases,
            "baseline_definition": "Executable conservative reference policy, not a replay of the entire legacy bot.",
            "model": "evidence-conditioned deterministic policy double; not provider quality evidence",
            "summary": {"case_count": len(cases), "passed": sum(case["passed"] for case in cases),
                        "resolved_count": sum(case["status"] == "resolved" for case in cases),
                        "human_escalation_count": sum(case["status"] == "waiting_human" for case in cases),
                        "wrong_action_count": sum(case["wrong_action_count"] for case in cases),
                        "duplicate_action_count": sum(case["duplicate_action_count"] for case in cases),
                        "tool_call_count": sum(case["tool_calls"] for case in cases),
                        "avg_tool_calls": sum(case["tool_calls"] for case in cases) / len(cases),
                        "loop_steps": sum(case["loop_steps"] for case in cases),
                        "latency_seconds": sum(case["latency_seconds"] for case in cases),
                        "model_tokens": None,
                        "timeouts": sum(case["status"] == "timed_out" for case in cases),
                        "budget_exhausted": sum(case["status"] == "budget_exhausted" for case in cases)}}
