from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .domain import Approval, DiagnosticPlan


class ApprovalService:
    VALID_DECISIONS = {"approve", "modify", "reject", "request_information"}

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
        approval = Approval(
            id=str(uuid.uuid4()), incident_id=incident_id, plan_version=plan.version,
            plan_digest=plan.digest(), object_fingerprint=plan.target.fingerprint,
            decision=decision, reviewer=reviewer, decided_at=datetime.now(timezone.utc),
            expires_at=expires_at, modified_parameters=modified_parameters, note=note,
        )
        return self.repository.save_approval(approval)

    def get_valid(self, incident_id: str, plan: DiagnosticPlan, current_fingerprint: str) -> Approval | None:
        now = datetime.now(timezone.utc)
        for item in reversed(self.repository.approvals_for(incident_id)):
            expires = item.expires_at
            if expires and not expires.tzinfo:
                expires = expires.replace(tzinfo=timezone.utc)
            if item.plan_version != plan.version or item.plan_digest != plan.digest():
                continue
            if item.object_fingerprint != current_fingerprint or item.decision != "approve":
                continue
            if expires and expires <= now:
                continue
            return item
        return None
