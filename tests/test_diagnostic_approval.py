import asyncio
from datetime import datetime, timedelta, timezone

from diagnostics.approval import ApprovalService
from diagnostics.domain import DiagnosticPlan, PlanTarget
from diagnostics.execution import ControlledExecutor, ExecutionHandler
from diagnostics.repository import InMemoryDiagnosticRepository


def plan(version=1):
    return DiagnosticPlan(
        version=version,
        target=PlanTarget("order", "abc", "fingerprint-v1"),
        action="sync_local_order",
        parameters={"state": "filled"},
        evidence_ids=["ev-1"],
        preconditions=["exchange order is still filled"],
        risk="medium",
        expected_result="local order is filled",
        verification=["local state is filled"],
    )


def test_approval_is_idempotent_and_bound_to_plan_version():
    repo = InMemoryDiagnosticRepository()
    approvals = ApprovalService(repo)
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    first = approvals.decide("inc-1", plan(), "approve", "alice", expires_at=expires)
    duplicate = approvals.decide("inc-1", plan(), "approve", "alice", expires_at=expires)

    assert duplicate.id == first.id
    assert approvals.get_valid("inc-1", plan(), "fingerprint-v1").decision == "approve"
    assert approvals.get_valid("inc-1", plan(version=2), "fingerprint-v1") is None


def test_changed_object_blocks_approved_execution():
    repo = InMemoryDiagnosticRepository()
    approvals = ApprovalService(repo)
    approvals.decide(
        "inc-1", plan(), "approve", "alice",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    calls = []

    async def execute(_):
        calls.append("executed")
        return {"ok": True}

    handler = ExecutionHandler(
        action="sync_local_order",
        risk_check=lambda _: (True, "ok"),
        fingerprint=lambda _: "fingerprint-v2",
        execute=execute,
        verify=lambda _: {"status": "satisfied"},
    )
    result = asyncio.run(ControlledExecutor(repo, approvals, [handler]).execute("inc-1", plan()))

    assert result.status == "stale_approval"
    assert calls == []


def test_execution_operation_id_prevents_duplicate_action():
    repo = InMemoryDiagnosticRepository()
    approvals = ApprovalService(repo)
    approvals.decide(
        "inc-1", plan(), "approve", "alice",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    calls = []

    async def execute(_):
        calls.append("executed")
        return {"ok": True}

    handler = ExecutionHandler(
        action="sync_local_order",
        risk_check=lambda _: (True, "ok"),
        fingerprint=lambda _: "fingerprint-v1",
        execute=execute,
        verify=lambda _: {"status": "satisfied"},
    )
    executor = ControlledExecutor(repo, approvals, [handler])
    one = asyncio.run(executor.execute("inc-1", plan()))
    two = asyncio.run(executor.execute("inc-1", plan()))

    assert one.status == "verified"
    assert two.operation_id == one.operation_id
    assert calls == ["executed"]


def test_review_supports_modify_reject_and_request_information():
    repo = InMemoryDiagnosticRepository()
    approvals = ApprovalService(repo)
    modified = approvals.decide("inc", plan(), "modify", "alice", modified_parameters={"state": "partially_filled"})
    rejected = approvals.decide("inc", plan(), "reject", "bob", note="wrong target")
    requested = approvals.decide("inc", plan(), "request_information", "carol", note="need fresh position")
    assert modified.modified_parameters == {"state": "partially_filled"}
    assert rejected.decision == "reject"
    assert requested.decision == "request_information"


def test_modify_creates_unapproved_plan_version():
    from dataclasses import asdict
    from diagnostics.domain import IncidentInput, utcnow
    repo = InMemoryDiagnosticRepository()
    item = repo.report(IncidentInput("modify", "unknown", "order", "abc", utcnow()))
    original = plan()
    repo.save_checkpoint(item.id, {"plan": asdict(original), "status": "waiting_human"})
    approvals = ApprovalService(repo)
    approvals.decide(item.id, original, "approve", "alice")
    approvals.decide(item.id, original, "modify", "alice", modified_parameters={"state": "partial"})
    revised = repo.load_checkpoint(item.id)["plan"]
    assert revised["version"] == 2
    assert revised["parameters"] == {"state": "partial"}
    assert approvals.get_valid(item.id, original, original.target.fingerprint) is None
