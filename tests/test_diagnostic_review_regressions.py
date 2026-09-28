import asyncio
from datetime import timedelta

from diagnostics.agent import AgentBudgets, DiagnosticAgent
from diagnostics.approval import ApprovalService
from diagnostics.domain import DiagnosticPlan, IncidentInput, PlanTarget, utcnow
from diagnostics.execution import ControlledExecutor, ExecutionHandler
from diagnostics.model import ScriptedDiagnosticModel
from diagnostics.repository import InMemoryDiagnosticRepository
from diagnostics.service import IncidentService
from diagnostics.tools import ToolRegistry


def setup():
    repo = InMemoryDiagnosticRepository()
    value = IncidentInput("review", "unknown", "order", "1", utcnow())
    item = repo.report(value)
    return repo, value, item


def test_model_hang_is_bounded():
    class Hanging:
        async def decide(self, *_):
            await asyncio.sleep(30)
    repo, _, item = setup()
    state = asyncio.run(DiagnosticAgent(repo, ToolRegistry([]), Hanging(), AgentBudgets(max_seconds=.02)).run(item.id))
    assert state.status == "timed_out"


def test_no_action_cannot_self_certify_resolution():
    repo, value, item = setup()
    agent = DiagnosticAgent(repo, ToolRegistry([]), ScriptedDiagnosticModel([
        {"kind": "plan", "plan": {"action": "no_action"}},
    ]))
    state = asyncio.run(agent.run(item.id))
    assert state.status == "waiting_human"
    service = IncidentService(repo, agent, enabled=True)
    service.report_nowait(value)
    assert service.queue.empty()
    assert asyncio.run(agent.run(item.id)).step == state.step


def test_open_incident_recovers_without_checkpoint():
    repo, _, item = setup()
    assert item.id in repo.resumable_ids()


def test_database_failure_does_not_escape_workflow_hook():
    class Broken:
        def report(self, _):
            raise RuntimeError("database unavailable")
    _, value, _ = setup()
    assert IncidentService(Broken(), enabled=True).report_nowait(value) is None


def test_array_arguments_are_rejected():
    record = asyncio.run(ToolRegistry([]).call("query_order", ["bad"]))
    assert record.result.error_type == "invalid_arguments"


def test_latest_rejection_revokes_approval():
    repo, _, item = setup()
    plan = DiagnosticPlan(1, PlanTarget("order", "1", "v1"), "sync", {}, [], [], "high", "synced", ["synced"])
    approval = ApprovalService(repo)
    approval.decide(item.id, plan, "approve", "alice", expires_at=utcnow()+timedelta(minutes=1))
    approval.decide(item.id, plan, "reject", "alice")
    assert approval.get_valid(item.id, plan, "v1") is None


def test_timeout_recovery_verifies_without_replaying_action():
    repo, _, item = setup()
    plan = DiagnosticPlan(1, PlanTarget("order", "1", "v1"), "sync", {}, [], [], "high", "synced", ["synced"])
    approval = ApprovalService(repo)
    approval.decide(item.id, plan, "approve", "alice")
    actions = []
    async def execute(_):
        actions.append("applied")
        raise TimeoutError("response lost")
    handler = ExecutionHandler("sync", lambda _: (True, "ok"), lambda _: "v1", execute, lambda _: {"status": "satisfied"})
    first = asyncio.run(ControlledExecutor(repo, approval, [handler]).execute(item.id, plan))
    second = asyncio.run(ControlledExecutor(repo, approval, [handler]).execute(item.id, plan))
    assert first.status == "pending_confirmation"
    assert second.status == "verified"
    assert actions == ["applied"]


def test_concurrent_callbacks_only_execute_once():
    repo, _, item = setup()
    plan = DiagnosticPlan(1, PlanTarget("order", "1", "v1"), "sync", {}, [], [], "high", "synced", ["synced"])
    approval = ApprovalService(repo)
    approval.decide(item.id, plan, "approve", "alice")
    actions = []
    async def execute(_):
        actions.append(1)
        await asyncio.sleep(.01)
        return {}
    handler = ExecutionHandler("sync", lambda _: (True, "ok"), lambda _: "v1", execute, lambda _: {"status": "satisfied"})
    async def run():
        await asyncio.gather(*(ControlledExecutor(repo, approval, [handler]).execute(item.id, plan) for _ in range(10)))
    asyncio.run(run())
    assert len(actions) == 1


def test_websocket_quiet_is_normal():
    from core.exchange_health import RuntimeHealth
    from diagnostics.health import classify, HealthStatus
    snapshot = RuntimeHealth()
    snapshot.ws.connected = True
    snapshot.trackers.last_rest_sync_ago = 1
    assert snapshot.ws.last_message_ago == float("inf")
    assert classify(snapshot) == HealthStatus.NORMAL


def test_websocket_disconnect_is_workflow_degradation():
    from core.exchange_health import RuntimeHealth
    from diagnostics.health import classify, HealthStatus
    assert classify(RuntimeHealth()) == HealthStatus.DEGRADED


def test_invalid_limits_never_reach_tool():
    from diagnostics.tools import DiagnosticTool
    async def forbidden(_):
        raise AssertionError("invalid query executed")
    tools = ToolRegistry([DiagnosticTool("fills", {"limit": int}, forbidden)])
    for invalid in (-1, 0, 101, True):
        assert asyncio.run(tools.call("fills", {"limit": invalid})).result.error_type == "invalid_arguments"


def test_sqlite_restart_restores_approval_and_claim(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import Base
    from diagnostics.repository import DiagnosticRepository
    engine = create_engine(f"sqlite:///{tmp_path / 'restart.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    repo = DiagnosticRepository(sessions)
    item = repo.report(IncidentInput("restart", "unknown", "order", "1", utcnow()))
    plan = DiagnosticPlan(1, PlanTarget("order", "1", "v1"), "sync", {}, [], [], "high", "synced", ["synced"])
    ApprovalService(repo).decide(item.id, plan, "approve", "alice")
    restarted = DiagnosticRepository(sessions)
    assert ApprovalService(restarted).get_valid(item.id, plan, "v1")
    assert restarted.get_incident(item.id).id == item.id
    now = utcnow()
    record = dict(operation_id="stable", incident_id=item.id, plan_version=1, action="sync", parameters={}, status="executing", created_at=now, updated_at=now, result=None, verification=None)
    assert repo.claim_execution(record)
    assert not restarted.claim_execution(record)


def test_restart_worker_verifies_health_and_reconciliation():
    from diagnostics.remediation import recovery_handler
    calls = []
    async def recover():
        calls.append("restart")
    async def health():
        calls.append("health")
        return dict.fromkeys(("alive", "authenticated", "subscribed", "snapshot_fresh", "exit_healthy"), True)
    async def reconcile():
        calls.append("reconcile")
        return True
    repo, _, item = setup()
    plan = DiagnosticPlan(1, PlanTarget("worker", "ws", "v1"), "restart_websocket_worker", {}, [], [], "high", "healthy", ["healthy"])
    approvals = ApprovalService(repo)
    approvals.decide(item.id, plan, "approve", "alice")
    handler = recovery_handler(plan.action, recover=recover, fingerprint=lambda _: "v1", safe=lambda _: (True, "safe"), health=health, reconcile=reconcile)
    result = asyncio.run(ControlledExecutor(repo, approvals, [handler]).execute(item.id, plan))
    assert result.status == "verified"
    assert calls == ["restart", "health", "reconcile"]
    assert repo.get_incident(item.id).status == "resolved"


def test_restart_consumes_existing_time_budget():
    from diagnostics.domain import AgentState
    repo, _, item = setup()
    state = AgentState(item, elapsed_seconds=61)
    repo.save_checkpoint(item.id, state.to_checkpoint())
    result = asyncio.run(DiagnosticAgent(repo, ToolRegistry([]), ScriptedDiagnosticModel([])).run(item.id))
    assert result.status == "timed_out"
    assert result.step == 0


def test_credentials_are_redacted():
    from diagnostics.redaction import redact
    assert redact({"api_key": "private", "log": "secret=private"}) == {"api_key": "[REDACTED]", "log": "secret=[REDACTED]"}
