from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .domain import DiagnosticPlan, ExecutionResult, stable_digest


@dataclass
class ExecutionHandler:
    action: str
    risk_check: Callable[[DiagnosticPlan], tuple[bool, str]]
    fingerprint: Callable[[DiagnosticPlan], str]
    execute: Callable[[DiagnosticPlan], Awaitable[dict[str, Any]]]
    verify: Callable[[DiagnosticPlan], dict[str, Any] | Awaitable[dict[str, Any]]]


class ControlledExecutor:
    def __init__(self, repository, approvals, handlers: list[ExecutionHandler]):
        self.repository = repository
        self.approvals = approvals
        self.handlers = {handler.action: handler for handler in handlers}

    async def execute(self, incident_id: str, plan: DiagnosticPlan) -> ExecutionResult:
        operation_id = stable_digest({"incident": incident_id, "plan": plan.digest()})[:32]
        existing = self.repository.get_execution(operation_id)
        if existing:
            return ExecutionResult(operation_id, existing["status"], existing.get("result") or {}, existing.get("verification") or {})
        handler = self.handlers.get(plan.action)
        if not handler:
            return ExecutionResult(operation_id, "rejected", {"reason": "action_not_allowed"})
        fingerprint = handler.fingerprint(plan)
        if not self.approvals.get_valid(incident_id, plan, fingerprint):
            status = "stale_approval" if plan.target.fingerprint and fingerprint != plan.target.fingerprint else "approval_required"
            return ExecutionResult(operation_id, status)
        allowed, reason = handler.risk_check(plan)
        if not allowed:
            return ExecutionResult(operation_id, "risk_rejected", {"reason": reason})

        now = datetime.now(timezone.utc)
        record = {
            "operation_id": operation_id, "incident_id": incident_id,
            "plan_version": plan.version, "action": plan.action,
            "parameters": plan.parameters, "status": "executing",
            "created_at": now, "updated_at": now, "result": None, "verification": None,
        }
        self.repository.save_execution(record)
        try:
            result = await handler.execute(plan)
            verification = handler.verify(plan)
            if inspect.isawaitable(verification):
                verification = await verification
            status = "verified" if verification.get("status") == "satisfied" else "pending_confirmation"
        except Exception as exc:
            result = {"error_type": type(exc).__name__, "message": str(exc)[:500]}
            verification = {"status": "unknown"}
            status = "pending_confirmation"
        record.update(status=status, updated_at=datetime.now(timezone.utc), result=result, verification=verification)
        self.repository.save_execution(record)
        return ExecutionResult(operation_id, status, result, verification)
