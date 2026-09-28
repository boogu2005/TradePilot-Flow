from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

from database.models import (
    DiagnosticApprovalRecord,
    DiagnosticExecutionRecord,
    DiagnosticIncidentRecord,
)
from .domain import Approval, Evidence, Incident, IncidentInput


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
    def __init__(self, session_factory: Callable):
        self._sessions = session_factory

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
                row.evidence = [*(row.evidence or []), *(item for item in evidence if item["id"] not in known)]
            session.commit()
            session.refresh(row)
            return _incident_from_record(row)

    def get_incident(self, incident_id: str) -> Incident:
        with self._sessions() as session:
            row = session.get(DiagnosticIncidentRecord, incident_id)
            if row is None:
                raise KeyError(incident_id)
            return _incident_from_record(row)

    def save_checkpoint(self, incident_id: str, checkpoint: dict[str, Any]) -> None:
        with self._sessions() as session:
            row = session.get(DiagnosticIncidentRecord, incident_id)
            if row is None:
                raise KeyError(incident_id)
            row.checkpoint = copy.deepcopy(checkpoint)
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
                DiagnosticIncidentRecord.checkpoint.is_not(None),
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


class InMemoryDiagnosticRepository:
    def __init__(self):
        self.incidents: dict[str, Incident] = {}
        self.by_key: dict[str, str] = {}
        self.checkpoints: dict[str, dict[str, Any]] = {}
        self.approvals: list[Approval] = []
        self.executions: dict[str, dict[str, Any]] = {}

    def report(self, value: IncidentInput) -> Incident:
        if value.correlation_key in self.by_key:
            item = self.incidents[self.by_key[value.correlation_key]]
            item.occurrences += 1
            item.last_seen_at = value.occurred_at
            item.recovery_steps = list(dict.fromkeys([*item.recovery_steps, *value.recovery_steps]))
            item.evidence.extend(value.evidence)
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

    def save_checkpoint(self, incident_id: str, checkpoint: dict[str, Any]) -> None:
        self.checkpoints[incident_id] = copy.deepcopy(checkpoint)

    def load_checkpoint(self, incident_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.checkpoints.get(incident_id))

    def set_status(self, incident_id: str, status: str) -> None:
        self.incidents[incident_id].status = status

    def resumable_ids(self) -> list[str]:
        return [incident_id for incident_id in self.checkpoints if self.incidents[incident_id].status in ("open", "investigating")]

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
