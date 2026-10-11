from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .domain import DiagnosticPlan, ExecutionResult, stable_digest
from .storage import repository_call


@dataclass
class ExecutionHandler:
    action: str
    risk_check: Callable[[DiagnosticPlan], tuple[bool, str] | Awaitable[tuple[bool, str]]]
    fingerprint: Callable[[DiagnosticPlan], str | Awaitable[str]]
    execute: Callable[[DiagnosticPlan], Awaitable[dict[str, Any]]]
    verify: Callable[[DiagnosticPlan], dict[str, Any] | Awaitable[dict[str, Any]]]
    timeout_seconds: float = 10.0


class ControlledExecutor:
    def __init__(self, repository, approvals, handlers: list[ExecutionHandler], verification_window_seconds: float = 120):
        self.repository = repository
        self.approvals = approvals
        self.handlers = {handler.action: handler for handler in handlers}
        self.verification_window_seconds = verification_window_seconds

    async def execute(self, incident_id: str, plan: DiagnosticPlan) -> ExecutionResult:
        operation_id = stable_digest({
            "incident": incident_id, "target_type": plan.target.type,
            "target_id": plan.target.id, "action": plan.action, "parameters": plan.parameters,
        })[:32]
        existing = await repository_call(self.repository, "get_execution", operation_id)
        if existing:
            handler = self.handlers.get(plan.action)
            if existing["status"] in ("executing", "pending_confirmation") and handler:
                created = existing["created_at"]
                if not created.tzinfo:
                    created = created.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - created).total_seconds() >= self.verification_window_seconds:
                    existing["status"] = "manual_review"
                    existing["verification"] = {"status": "unknown", "reason": "verification_deadline_exceeded"}
                    await repository_call(self.repository, "save_execution", existing)
                    await repository_call(self.repository, "set_status", incident_id, "waiting_human")
                    return ExecutionResult(operation_id, "manual_review", existing.get("result") or {}, existing["verification"])
                try:
                    verification = handler.verify(plan)
                    if inspect.isawaitable(verification):
                        verification = await asyncio.wait_for(verification, handler.timeout_seconds)
                    existing["verification"] = verification
                    existing["status"] = "verified" if verification.get("status") == "satisfied" else "pending_confirmation"
                    existing["updated_at"] = datetime.now(timezone.utc)
                    await repository_call(self.repository, "save_execution", existing)
                    if existing["status"] == "verified":
                        await repository_call(self.repository, "set_status", incident_id, "resolved")
                except Exception as exc:  # noqa: BLE001 - never replay uncertain side effects
                    existing["verification"] = {"status": "unknown", "error_type": type(exc).__name__}
                    existing["status"] = "pending_confirmation"
                    await repository_call(self.repository, "save_execution", existing)
            return ExecutionResult(operation_id, existing["status"], existing.get("result") or {}, existing.get("verification") or {})
        handler = self.handlers.get(plan.action)
        if not handler:
            return ExecutionResult(operation_id, "rejected", {"reason": "action_not_allowed"})
        checkpoint = await repository_call(self.repository, "load_checkpoint", incident_id)
        if checkpoint and checkpoint.get("plan") and checkpoint["plan"] != asdict(plan):
            return ExecutionResult(operation_id, "stale_approval", {"reason": "plan_replaced"})
        fingerprint = handler.fingerprint(plan)
        if inspect.isawaitable(fingerprint):
            fingerprint = await asyncio.wait_for(fingerprint, handler.timeout_seconds)
        approval = await asyncio.to_thread(self.approvals.get_valid, incident_id, plan, fingerprint)
        if not approval:
            status = "stale_approval" if plan.target.fingerprint and fingerprint != plan.target.fingerprint else "approval_required"
            return ExecutionResult(operation_id, status)
        checked = handler.risk_check(plan)
        if inspect.isawaitable(checked):
            checked = await asyncio.wait_for(checked, handler.timeout_seconds)
        allowed, reason = checked
        if not allowed:
            return ExecutionResult(operation_id, "risk_rejected", {"reason": reason})

        # Risk checks may perform slow REST reads. Revalidate after those reads.
        latest = handler.fingerprint(plan)
        if inspect.isawaitable(latest):
            latest = await asyncio.wait_for(latest, handler.timeout_seconds)
        if latest != fingerprint or not await asyncio.to_thread(self.approvals.get_valid, incident_id, plan, latest):
            return ExecutionResult(operation_id, "stale_approval")

        now = datetime.now(timezone.utc)
        record = {
            "operation_id": operation_id, "incident_id": incident_id,
            "plan_version": plan.version, "action": plan.action,
            "parameters": plan.parameters, "status": "executing",
            "created_at": now, "updated_at": now, "result": None, "verification": None,
        }
        if not await repository_call(self.repository, "claim_execution", record):
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
        await repository_call(self.repository, "save_execution", record)
        if status == "verified":
            await repository_call(self.repository, "set_status", incident_id, "resolved")
        return ExecutionResult(operation_id, status, result, verification)
