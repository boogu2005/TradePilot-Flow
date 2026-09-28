from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from .agent import DiagnosticAgent
from .approval import ApprovalService
from .domain import Evidence, IncidentInput, ToolResult
from .execution import ControlledExecutor, ExecutionHandler
from .repository import InMemoryDiagnosticRepository
from .tools import DiagnosticTool, ToolRegistry


async def _run_demo() -> dict:
    repository = InMemoryDiagnosticRepository()
    incident = repository.report(IncidentInput(
        correlation_key="demo:order-timeout-001",
        event_type="order_result_unknown",
        object_type="order", object_id="demo-order-001",
        occurred_at=datetime.now(timezone.utc),
        recovery_steps=["submit_timeout", "normal_rest_query_unknown"],
        evidence=[Evidence("workflow", "timeout", {"request_id": "req-001"})],
    ))
    local_state = {"order_id": "demo-order-001", "state": "pending", "fingerprint": "order-v1"}

    async def query_exchange(_):
        return ToolResult.ok("exchange_rest", {"order_id": "demo-order-001", "state": "closed", "filled": 1.0})

    async def query_local(_):
        return ToolResult.ok("local_database", dict(local_state))

    tools = ToolRegistry([
        DiagnosticTool("query_order", {"order_id": str}, query_exchange),
        DiagnosticTool("query_local", {"order_id": str}, query_local),
    ])
    decisions = [
        {"kind": "tool", "tool": "query_order", "arguments": {"order_id": "demo-order-001"}},
        {"kind": "tool", "tool": "query_local", "arguments": {"order_id": "demo-order-001"}},
        {"kind": "plan", "candidate_causes": [{"cause": "exchange_response_lost", "because": "exchange filled while local stayed pending"}],
         "plan": {"version": 1, "target": {"type": "order", "id": "demo-order-001", "fingerprint": "order-v1"},
                  "action": "sync_local_order", "parameters": {"state": "closed", "filled": 1.0},
                  "evidence_ids": ["exchange-query", "local-query"],
                  "preconditions": ["exchange order remains closed and filled"], "risk": "medium",
                  "expected_result": "local order matches exchange", "verification": ["local state is closed", "local filled is 1.0"]}},
    ]

    class EvidenceDrivenDemoModel:
        """Offline policy double: every transition depends on returned evidence."""
        async def decide(self, current, _tools):
            if not current.tool_calls:
                return decisions[0]
            observation = current.tool_calls[-1].result
            if observation.status != "ok":
                return {"kind": "escalate", "reason": "query failed; order outcome unknown"}
            if current.tool_calls[-1].tool == "query_order":
                if observation.data.get("state") != "closed":
                    return {"kind": "escalate", "reason": "exchange order is not confirmed filled"}
                return decisions[1]
            if observation.data.get("state") != "pending":
                return {"kind": "escalate", "reason": "local state changed; new review required"}
            return decisions[2]

    model = EvidenceDrivenDemoModel()
    state = await DiagnosticAgent(repository, tools, model).run(incident.id)
    assert state.plan is not None

    approvals = ApprovalService(repository)
    approval = approvals.decide(
        incident.id, state.plan, "approve", "demo-reviewer",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        note="offline demo approval",
    )

    async def execute(plan):
        local_state.update(plan.parameters)
        return {"updated": True, "order_id": local_state["order_id"]}

    def verify(_):
        satisfied = local_state.get("state") == "closed" and local_state.get("filled") == 1.0
        return {"status": "satisfied" if satisfied else "unknown", "local_state": dict(local_state)}

    handler = ExecutionHandler(
        "sync_local_order", lambda _: (True, "no new exposure"),
        lambda _: local_state["fingerprint"], execute, verify,
    )
    execution = await ControlledExecutor(repository, approvals, [handler]).execute(incident.id, state.plan)
    return {
        "stages": ["incident_persisted", "evidence_queried", "plan_ready", "human_approved", "controlled_execution", "business_verified"],
        "incident": {"id": incident.id, "correlation_key": incident.correlation_key,
                     "status": repository.get_incident(incident.id).status},
        "agent": {"status": state.status, "termination": state.termination_reason, "plan": state.plan.action},
        "approval": {"id": approval.id, "reviewer": approval.reviewer, "plan_version": approval.plan_version},
        "execution": {"operation_id": execution.operation_id, "status": execution.status, "verification": execution.verification},
        "audit": {"tool_calls": len(state.tool_calls), "steps": state.step, "token_usage": state.token_usage},
    }


def run_demo() -> dict:
    return asyncio.run(_run_demo())


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
