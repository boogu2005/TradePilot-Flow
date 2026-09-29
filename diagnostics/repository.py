from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from database.models import (
    DiagnosticApprovalRecord,
    DiagnosticExecutionRecord,
    DiagnosticIncidentRecord,
    DiagnosticInvestigationRecord,
    DiagnosticLeaseRecord,
)

from .domain import Approval, Evidence, Incident, IncidentInput, InvestigationBusy


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _incident_from_record(row: DiagnosticIncidentRecord) -> Incident:
    return Incident(
        id=row.id,
        correlation_key=row.correlation_key,
        event_type=row.event_type,
        object_type=row.object_type,
        object_id=row.object_id,
        occurred_at=_aware(row.occurred_at),
        last_seen_at=_aware(row.last_seen_at),
        status=row.status,
        occurrences=row.occurrences,
        recovery_steps=list(row.recovery_steps or []),
        evidence=[Evidence.from_dict(item) for item in (row.evidence or [])],
    )


class DiagnosticRepository:
    blocking_io = True
    def __init__(self, session_factory: Callable):
        self._sessions = session_factory

    def claim_lease(self, key: str, owner: str, seconds: float) -> bool:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=seconds)
        with self._sessions() as session:
            changed = session.execute(update(DiagnosticLeaseRecord).where(
                DiagnosticLeaseRecord.key == key, DiagnosticLeaseRecord.expires_at <= now,
            ).values(owner=owner, expires_at=expires)).rowcount
            if changed:
                session.commit()
                return True
            session.add(DiagnosticLeaseRecord(key=key, owner=owner, expires_at=expires))
            try:
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                return False

    def release_lease(self, key: str, owner: str) -> None:
        with self._sessions() as session:
            session.execute(delete(DiagnosticLeaseRecord).where(
                DiagnosticLeaseRecord.key == key, DiagnosticLeaseRecord.owner == owner,
            ))
            session.commit()

    def executions_for(self, incident_id: str) -> list[dict]:
        with self._sessions() as session:
            rows = session.scalars(select(DiagnosticExecutionRecord).where(
                DiagnosticExecutionRecord.incident_id == incident_id,
            )).all()
            return [{column.name: getattr(row, column.name) for column in row.__table__.columns} for row in rows]

    def reopen(self, incident_id: str, information: dict) -> Incident:
        with self._sessions() as session:
            row = session.get(DiagnosticIncidentRecord, incident_id)
            if row is None:
                raise KeyError(incident_id)
            if row.status in ("open", "investigating"):
                raise ValueError("investigation is already active")
            uncertain = session.scalar(select(DiagnosticExecutionRecord.operation_id).where(
                DiagnosticExecutionRecord.incident_id == incident_id,
                DiagnosticExecutionRecord.status.in_(("executing", "pending_confirmation")),
            ))
            if uncertain:
                raise ValueError("verify uncertain execution before reopening")
            checkpoint = dict(row.checkpoint or {})
            run_id = checkpoint.get("agent_run_id", uuid.uuid4().hex)
            if not session.get(DiagnosticInvestigationRecord, run_id):
                session.add(DiagnosticInvestigationRecord(run_id=run_id, incident_id=incident_id, checkpoint=copy.deepcopy(checkpoint)))
            previous_version = (checkpoint.get("plan") or {}).get("version", checkpoint.get("plan_version_floor", 1))
            checkpoint["plan_version_floor"] = int(previous_version or 1) + 1
            checkpoint["status"] = "investigating"
            checkpoint["termination_reason"] = None
            checkpoint["step"] = 0
            checkpoint["elapsed_seconds"] = 0
            checkpoint["token_usage"] = 0
            checkpoint["tool_calls"] = []
            checkpoint["plan"] = None
            checkpoint["agent_run_id"] = uuid.uuid4().hex
            checkpoint["started_at"] = datetime.now(timezone.utc).isoformat()
            row.checkpoint = checkpoint
            row.status = "open"
            row.evidence = [*(row.evidence or []), Evidence("human", "additional_information", information).to_dict()][-100:]
            session.commit()
            return _incident_from_record(row)

    def investigations_for(self, incident_id: str) -> list[dict]:
        with self._sessions() as session:
            rows = session.scalars(select(DiagnosticInvestigationRecord).where(
                DiagnosticInvestigationRecord.incident_id == incident_id,
            ).order_by(DiagnosticInvestigationRecord.archived_at)).all()
            return [{"run_id": row.run_id, "checkpoint": row.checkpoint, "archived_at": row.archived_at} for row in rows]

    def report(self, value: IncidentInput) -> Incident:
        with self._sessions() as session:
            row = session.scalar(select(DiagnosticIncidentRecord).where(
                DiagnosticIncidentRecord.correlation_key == value.correlation_key
            ))
            evidence = [item.to_dict() for item in value.evidence]
            if row is None:
                row = DiagnosticIncidentRecord(
                    id=str(uuid.uuid4()), correlation_key=value.correlation_key,
                    event_type=value.event_type, object_type=value.object_type,
                    object_id=str(value.object_id), occurred_at=value.occurred_at,
                    last_seen_at=value.occurred_at, status="open", occurrences=1,
                    recovery_steps=list(dict.fromkeys(value.recovery_steps)), evidence=evidence,
                )
                session.add(row)
            else:
                row.last_seen_at = value.occurred_at
                row.occurrences += 1
                row.recovery_steps = list(dict.fromkeys([*(row.recovery_steps or []), *value.recovery_steps]))
                known = {item["id"] for item in (row.evidence or [])}
                row.evidence = [*(row.evidence or []), *(item for item in evidence if item["id"] not in known)][-100:]
            session.commit()
            session.refresh(row)
            return _incident_from_record(row)

    def get_incident(self, incident_id: str) -> Incident:
        with self._sessions() as session:
            row = session.get(DiagnosticIncidentRecord, incident_id)
            if row is None:
                raise KeyError(incident_id)
            return _incident_from_record(row)

    def save_checkpoint(self, incident_id: str, checkpoint: dict[str, Any], lease_owner: str | None = None) -> None:
        with self._sessions() as session:
            if lease_owner is not None:
                lease = select(DiagnosticLeaseRecord.key).where(
                    DiagnosticLeaseRecord.key == incident_id,
                    DiagnosticLeaseRecord.owner == lease_owner,
                    DiagnosticLeaseRecord.expires_at > datetime.now(timezone.utc),
                ).exists()
                changed = session.execute(update(DiagnosticIncidentRecord).where(
                    DiagnosticIncidentRecord.id == incident_id, lease,
                ).values(checkpoint=copy.deepcopy(checkpoint), status=checkpoint["status"])).rowcount
                if not changed:
                    session.rollback()
                    raise InvestigationBusy("checkpoint rejected: lease lost")
                session.commit()
                return
            row = session.get(DiagnosticIncidentRecord, incident_id)
            if row is None:
                raise KeyError(incident_id)
            row.checkpoint = copy.deepcopy(checkpoint)
            row.status = checkpoint.get("status", row.status)
            session.commit()

    def load_checkpoint(self, incident_id: str) -> dict[str, Any] | None:
        with self._sessions() as session:
            row = session.get(DiagnosticIncidentRecord, incident_id)
            return copy.deepcopy(row.checkpoint) if row and row.checkpoint else None

    def set_status(self, incident_id: str, status: str) -> None:
        with self._sessions() as session:
            row = session.get(DiagnosticIncidentRecord, incident_id)
            if row:
                row.status = status
                session.commit()

    def resumable_ids(self) -> list[str]:
        with self._sessions() as session:
            return list(session.scalars(select(DiagnosticIncidentRecord.id).where(
                DiagnosticIncidentRecord.status.in_(("open", "investigating")),
            )).all())

    def list_incidents(self, limit: int = 50) -> list[Incident]:
        with self._sessions() as session:
            rows = session.scalars(select(DiagnosticIncidentRecord).order_by(
                DiagnosticIncidentRecord.last_seen_at.desc()
            ).limit(max(1, min(limit, 200)))).all()
            return [_incident_from_record(row) for row in rows]

    def save_approval(self, approval: Approval) -> Approval:
        with self._sessions() as session:
            existing = session.scalar(select(DiagnosticApprovalRecord).where(
                DiagnosticApprovalRecord.incident_id == approval.incident_id,
                DiagnosticApprovalRecord.plan_version == approval.plan_version,
                DiagnosticApprovalRecord.plan_digest == approval.plan_digest,
                DiagnosticApprovalRecord.decision == approval.decision,
            ))
            if existing:
                return self._approval(existing)
            row = DiagnosticApprovalRecord(**approval.__dict__)
            session.add(row)
            session.commit()
            return approval

    def approvals_for(self, incident_id: str) -> list[Approval]:
        with self._sessions() as session:
            rows = session.scalars(select(DiagnosticApprovalRecord).where(
                DiagnosticApprovalRecord.incident_id == incident_id
            )).all()
            return [self._approval(row) for row in rows]

    @staticmethod
    def _approval(row: DiagnosticApprovalRecord) -> Approval:
        return Approval(
            id=row.id, incident_id=row.incident_id, plan_version=row.plan_version,
            plan_digest=row.plan_digest, object_fingerprint=row.object_fingerprint,
            decision=row.decision, reviewer=row.reviewer, decided_at=_aware(row.decided_at),
            expires_at=_aware(row.expires_at) if row.expires_at else None,
            modified_parameters=row.modified_parameters, note=row.note,
        )

    def get_execution(self, operation_id: str) -> dict[str, Any] | None:
        with self._sessions() as session:
            row = session.get(DiagnosticExecutionRecord, operation_id)
            if not row:
                return None
            return {column.name: getattr(row, column.name) for column in row.__table__.columns}

    def save_execution(self, value: dict[str, Any]) -> None:
        with self._sessions() as session:
            row = session.get(DiagnosticExecutionRecord, value["operation_id"])
            if row is None:
                row = DiagnosticExecutionRecord(**value)
                session.add(row)
            else:
                for key, item in value.items():
                    setattr(row, key, item)
            session.commit()

    def claim_execution(self, value: dict[str, Any]) -> bool:
        """The unique operation primary key arbitrates concurrent executors."""
        with self._sessions() as session:
            session.add(DiagnosticExecutionRecord(**value))
            try:
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                if session.get(DiagnosticExecutionRecord, value["operation_id"]) is None:
                    raise
                return False


class InMemoryDiagnosticRepository:
    def __init__(self):
        self.incidents: dict[str, Incident] = {}
        self.by_key: dict[str, str] = {}
        self.checkpoints: dict[str, dict[str, Any]] = {}
        self.approvals: list[Approval] = []
        self.executions: dict[str, dict[str, Any]] = {}
        self.leases: dict[str, tuple[str, datetime]] = {}

    def claim_lease(self, key: str, owner: str, seconds: float) -> bool:
        now = datetime.now(timezone.utc)
        if key in self.leases and self.leases[key][1] > now:
            return False
        self.leases[key] = (owner, now + timedelta(seconds=seconds))
        return True

    def release_lease(self, key: str, owner: str) -> None:
        if key in self.leases and self.leases[key][0] == owner:
            del self.leases[key]

    def executions_for(self, incident_id: str) -> list[dict]:
        return [copy.deepcopy(row) for row in self.executions.values() if row["incident_id"] == incident_id]

    def report(self, value: IncidentInput) -> Incident:
        if value.correlation_key in self.by_key:
            item = self.incidents[self.by_key[value.correlation_key]]
            item.occurrences += 1
            item.last_seen_at = value.occurred_at
            item.recovery_steps = list(dict.fromkeys([*item.recovery_steps, *value.recovery_steps]))
            item.evidence.extend(value.evidence)
            item.evidence = item.evidence[-100:]
            return copy.deepcopy(item)
        item = Incident(
            id=str(uuid.uuid4()), correlation_key=value.correlation_key,
            event_type=value.event_type, object_type=value.object_type,
            object_id=str(value.object_id), occurred_at=value.occurred_at,
            last_seen_at=value.occurred_at, recovery_steps=list(value.recovery_steps),
            evidence=list(value.evidence),
        )
        self.incidents[item.id] = item
        self.by_key[item.correlation_key] = item.id
        return copy.deepcopy(item)

    def get_incident(self, incident_id: str) -> Incident:
        return copy.deepcopy(self.incidents[incident_id])

    def save_checkpoint(self, incident_id: str, checkpoint: dict[str, Any], lease_owner: str | None = None) -> None:
        if lease_owner is not None:
            lease = self.leases.get(incident_id)
            if lease is None or lease[0] != lease_owner or lease[1] <= datetime.now(timezone.utc):
                raise InvestigationBusy("checkpoint rejected: lease lost")
        self.checkpoints[incident_id] = copy.deepcopy(checkpoint)
        self.incidents[incident_id].status = checkpoint.get("status", self.incidents[incident_id].status)

    def load_checkpoint(self, incident_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.checkpoints.get(incident_id))

    def set_status(self, incident_id: str, status: str) -> None:
        if incident_id in self.incidents:
            self.incidents[incident_id].status = status

    def resumable_ids(self) -> list[str]:
        return [key for key, item in self.incidents.items() if item.status in ("open", "investigating")]

    def list_incidents(self, limit: int = 50) -> list[Incident]:
        return [copy.deepcopy(item) for item in list(self.incidents.values())[-limit:]][::-1]

    def save_approval(self, approval: Approval) -> Approval:
        for item in self.approvals:
            if (item.incident_id, item.plan_version, item.plan_digest, item.decision) == (
                approval.incident_id, approval.plan_version, approval.plan_digest, approval.decision
            ):
                return copy.deepcopy(item)
        self.approvals.append(copy.deepcopy(approval))
        return approval

    def approvals_for(self, incident_id: str) -> list[Approval]:
        return [copy.deepcopy(item) for item in self.approvals if item.incident_id == incident_id]

    def get_execution(self, operation_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.executions.get(operation_id))

    def save_execution(self, value: dict[str, Any]) -> None:
        self.executions[value["operation_id"]] = copy.deepcopy(value)

    def claim_execution(self, value: dict[str, Any]) -> bool:
        if value["operation_id"] in self.executions:
            return False
        self.save_execution(value)
        return True
