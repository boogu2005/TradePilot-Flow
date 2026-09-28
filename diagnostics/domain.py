from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stable_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Evidence:
    source: str
    kind: str
    data: dict[str, Any]
    observed_at: datetime = field(default_factory=utcnow)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["observed_at"] = self.observed_at.isoformat()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Evidence":
        raw = dict(value)
        raw["observed_at"] = datetime.fromisoformat(raw["observed_at"])
        return cls(**raw)


@dataclass
class IncidentInput:
    correlation_key: str
    event_type: str
    object_type: str
    object_id: str
    occurred_at: datetime
    recovery_steps: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Incident:
    id: str
    correlation_key: str
    event_type: str
    object_type: str
    object_id: str
    occurred_at: datetime
    last_seen_at: datetime
    status: str = "open"
    occurrences: int = 1
    recovery_steps: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class ToolResult:
    status: str
    source: str
    observed_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    error_type: str | None = None
    message: str | None = None

    @classmethod
    def ok(cls, source: str, data: dict[str, Any]) -> "ToolResult":
        return cls("ok", source, utcnow(), data)

    @classmethod
    def not_found(cls, source: str, data: dict[str, Any] | None = None) -> "ToolResult":
        return cls("not_found", source, utcnow(), data or {})

    @classmethod
    def unknown(cls, source: str, data: dict[str, Any] | None = None, *, retryable: bool = False) -> "ToolResult":
        return cls("unknown", source, utcnow(), data or {}, retryable=retryable)

    @classmethod
    def error(cls, source: str, error_type: str, message: str, *, retryable: bool = False) -> "ToolResult":
        return cls("error", source, utcnow(), {}, retryable=retryable, error_type=error_type, message=message)

    def normalized_digest(self) -> str:
        return stable_digest({"status": self.status, "source": self.source, "data": self.data, "error_type": self.error_type})


@dataclass
class ToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    result: ToolResult
    started_at: datetime
    duration_ms: int


@dataclass
class PlanTarget:
    type: str
    id: str
    fingerprint: str = ""


@dataclass
class DiagnosticPlan:
    version: int
    target: PlanTarget
    action: str
    parameters: dict[str, Any]
    evidence_ids: list[str]
    preconditions: list[str]
    risk: str
    expected_result: str
    verification: list[str]

    def digest(self) -> str:
        return stable_digest(asdict(self))


@dataclass
class AgentState:
    incident: Incident
    status: str = "investigating"
    step: int = 0
    started_at: datetime = field(default_factory=utcnow)
    elapsed_seconds: float = 0.0
    token_usage: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    candidate_causes: list[dict[str, str]] = field(default_factory=list)
    pending_information: list[str] = field(default_factory=list)
    plan: DiagnosticPlan | None = None
    termination_reason: str | None = None

    @classmethod
    def from_checkpoint(cls, incident: Incident, value: dict[str, Any]) -> "AgentState":
        plan = None
        if value.get("plan"):
            raw = value["plan"]
            plan = DiagnosticPlan(
                version=raw["version"], target=PlanTarget(**raw["target"]),
                action=raw["action"], parameters=raw["parameters"],
                evidence_ids=raw["evidence_ids"], preconditions=raw["preconditions"],
                risk=raw["risk"], expected_result=raw["expected_result"],
                verification=raw["verification"],
            )
        calls = []
        for item in value.get("tool_calls", []):
            result = dict(item["result"])
            result["observed_at"] = datetime.fromisoformat(result["observed_at"])
            calls.append(ToolCallRecord(
                tool=item["tool"], arguments=item["arguments"], result=ToolResult(**result),
                started_at=datetime.fromisoformat(item["started_at"]), duration_ms=item["duration_ms"],
            ))
        return cls(
            incident=incident, status=value.get("status", "investigating"),
            step=int(value.get("step", 0)),
            started_at=datetime.fromisoformat(value["started_at"]),
            elapsed_seconds=float(value.get("elapsed_seconds", 0)),
            token_usage=int(value.get("token_usage", 0)), tool_calls=calls,
            candidate_causes=list(value.get("candidate_causes") or []),
            pending_information=list(value.get("pending_information") or []),
            plan=plan, termination_reason=value.get("termination_reason"),
        )

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident.id, "status": self.status, "step": self.step,
            "started_at": self.started_at.isoformat(), "elapsed_seconds": self.elapsed_seconds,
            "token_usage": self.token_usage,
            "candidate_causes": self.candidate_causes,
            "pending_information": self.pending_information,
            "plan": asdict(self.plan) if self.plan else None,
            "termination_reason": self.termination_reason,
            "tool_calls": [{
                "tool": c.tool, "arguments": c.arguments, "result": {
                    "status": c.result.status, "source": c.result.source,
                    "observed_at": c.result.observed_at.isoformat(), "data": c.result.data,
                    "retryable": c.result.retryable, "error_type": c.result.error_type,
                    "message": c.result.message,
                },
                "started_at": c.started_at.isoformat(), "duration_ms": c.duration_ms,
            } for c in self.tool_calls],
        }


@dataclass
class Approval:
    id: str
    incident_id: str
    plan_version: int
    plan_digest: str
    object_fingerprint: str
    decision: str
    reviewer: str
    decided_at: datetime
    expires_at: datetime | None = None
    modified_parameters: dict[str, Any] | None = None
    note: str | None = None


@dataclass
class ExecutionResult:
    operation_id: str
    status: str
    result: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
