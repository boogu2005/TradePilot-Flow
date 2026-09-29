from __future__ import annotations

import uuid
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

from .domain import Approval, DiagnosticPlan


class ApprovalService:
    VALID_DECISIONS = frozenset({"approve", "modify", "reject", "request_information"})

    def __init__(self, repository):
        self.repository = repository

    def decide(
        self, incident_id: str, plan: DiagnosticPlan, decision: str, reviewer: str,
        *, expires_at: datetime | None = None, modified_parameters: dict | None = None,
        note: str | None = None,
    ) -> Approval:
        if decision not in self.VALID_DECISIONS:
            raise ValueError(f"unsupported decision: {decision}")
        if decision == "modify" and not modified_parameters:
            raise ValueError("modified_parameters are required")
        if not reviewer.strip():
            raise ValueError("reviewer is required")
        if decision == "approve" and expires_at is None:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        approval = Approval(
            id=str(uuid.uuid4()), incident_id=incident_id, plan_version=plan.version,
            plan_digest=plan.digest(), object_fingerprint=plan.target.fingerprint,
            decision=decision, reviewer=reviewer, decided_at=datetime.now(timezone.utc),
            expires_at=expires_at, modified_parameters=modified_parameters, note=note,
        )
        saved = self.repository.save_approval(approval)
        if decision == "modify":
            checkpoint = self.repository.load_checkpoint(incident_id)
            if checkpoint and checkpoint.get("plan") == asdict(plan):
                revised = replace(plan, version=plan.version + 1, parameters=dict(modified_parameters or {}))
                checkpoint["plan"] = asdict(revised)
                checkpoint["status"] = "waiting_human"
                checkpoint["termination_reason"] = "modified_plan_requires_approval"
                self.repository.save_checkpoint(incident_id, checkpoint)
        return saved

    def get_valid(self, incident_id: str, plan: DiagnosticPlan, current_fingerprint: str) -> Approval | None:
        now = datetime.now(timezone.utc)
        decisions = self.repository.approvals_for(incident_id)
        for _, item in sorted(enumerate(decisions), key=lambda pair: (pair[1].decided_at, pair[0]), reverse=True):
            expires = item.expires_at
            if expires and not expires.tzinfo:
                expires = expires.replace(tzinfo=timezone.utc)
            if item.plan_version != plan.version or item.plan_digest != plan.digest():
                continue
            if item.object_fingerprint != current_fingerprint or item.decision != "approve":
                return None
            if expires and expires <= now:
                return None
            return item
        return None
