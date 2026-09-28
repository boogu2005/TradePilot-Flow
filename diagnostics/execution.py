from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .domain import DiagnosticPlan, ExecutionResult, stable_digest


@dataclass
class ExecutionHandler:
    action: str
    risk_check: Callable[[DiagnosticPlan], tuple[bool, str]]
    fingerprint: Callable[[DiagnosticPlan], str]
    execute: Callable[[DiagnosticPlan], Awaitable[dict[str, Any]]]
    verify: Callable[[DiagnosticPlan], dict[str, Any] | Awaitable[dict[str, Any]]]
    timeout_seconds: float = 10.0


class ControlledExecutor:
    def __init__(self, repository, approvals, handlers: list[ExecutionHandler]):
        self.repository = repository
        self.approvals = approvals
        self.handlers = {handler.action: handler for handler in handlers}

    async def execute(self, incident_id: str, plan: DiagnosticPlan) -> ExecutionResult:
        operation_id = stable_digest({
            "incident": incident_id, "target_type": plan.target.type,
            "target_id": plan.target.id, "action": plan.action, "parameters": plan.parameters,
        })[:32]
        existing = self.repository.get_execution(operation_id)
        if existing:
            handler = self.handlers.get(plan.action)
            if existing["status"] in ("executing", "pending_confirmation") and handler:
                try:
                    verification = handler.verify(plan)
                    if inspect.isawaitable(verification):
                        verification = await asyncio.wait_for(verification, handler.timeout_seconds)
                    existing["verification"] = verification
                    existing["status"] = "verified" if verification.get("status") == "satisfied" else "pending_confirmation"
                    existing["updated_at"] = datetime.now(timezone.utc)
                    self.repository.save_execution(existing)
                    if existing["status"] == "verified":
                        self.repository.set_status(incident_id, "resolved")
                except Exception as exc:  # noqa: BLE001 - never replay uncertain side effects
                    existing["verification"] = {"status": "unknown", "error_type": type(exc).__name__}
                    existing["status"] = "pending_confirmation"
                    self.repository.save_execution(existing)
            return ExecutionResult(operation_id, existing["status"], existing.get("result") or {}, existing.get("verification") or {})
        handler = self.handlers.get(plan.action)
        if not handler:
            return ExecutionResult(operation_id, "rejected", {"reason": "action_not_allowed"})
        checkpoint = self.repository.load_checkpoint(incident_id)
        if checkpoint and checkpoint.get("plan") and checkpoint["plan"] != asdict(plan):
            return ExecutionResult(operation_id, "stale_approval", {"reason": "plan_replaced"})
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
        if not self.repository.claim_execution(record):
            return ExecutionResult(operation_id, "pending_confirmation")
        try:
            result = await asyncio.wait_for(handler.execute(plan), handler.timeout_seconds)
            verification = handler.verify(plan)
            if inspect.isawaitable(verification):
                verification = await asyncio.wait_for(verification, handler.timeout_seconds)
            status = "verified" if verification.get("status") == "satisfied" else "pending_confirmation"
        except Exception as exc:  # noqa: BLE001 - preserve unknown side-effect outcomes
            result = {"error_type": type(exc).__name__, "message": str(exc)[:500]}
            verification = {"status": "unknown"}
            status = "pending_confirmation"
        record.update(status=status, updated_at=datetime.now(timezone.utc), result=result, verification=verification)
        self.repository.save_execution(record)
        if status == "verified":
            self.repository.set_status(incident_id, "resolved")
        return ExecutionResult(operation_id, status, result, verification)
