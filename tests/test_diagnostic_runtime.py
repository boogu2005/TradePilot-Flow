import asyncio
import json
import subprocess
import sys
import time
from types import SimpleNamespace

from diagnostics.agent import DiagnosticAgent, InvestigationBusy
from diagnostics.approval import ApprovalService
from diagnostics.domain import DiagnosticPlan, IncidentInput, PlanTarget, utcnow
from diagnostics.execution import ControlledExecutor, ExecutionHandler
from diagnostics.model import ScriptedDiagnosticModel
from diagnostics.repository import InMemoryDiagnosticRepository
from diagnostics.runtime_control import RuntimeControl
from diagnostics.sandbox import Sandbox
from diagnostics.tools import ToolRegistry


def test_sandbox_survives_separate_processes_and_never_replays(tmp_path):
    db = tmp_path / "sandbox.db"
    def run(command):
        result = subprocess.run([sys.executable, "-m", "diagnostics.sandbox", "--db", str(db), command],
                                capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
        return json.loads(result.stdout)
    run("init")
    assert run("investigate")["status"] == "waiting_human"
    assert run("execute")["status"] == "approval_required"
    run("review")
    assert run("execute")["status"] == "verified"
    assert run("execute")["status"] == "verified"
    trace = run("status")
    assert len(trace["fake_exchange"]["actions"]) == 1
    assert trace["incident"]["status"] == "resolved"
    assert len(trace["checkpoint"]["tool_calls"]) >= 2


def test_busy_investigation_does_not_mutate_checkpoint():
    async def scenario():
        repo = InMemoryDiagnosticRepository()
        item = repo.report(IncidentInput("busy", "unknown", "order", "1", utcnow()))
        assert repo.claim_lease(item.id, "worker1", 60)
        agent = DiagnosticAgent(repo, ToolRegistry([]), ScriptedDiagnosticModel([]))
        try:
            await agent.run(item.id)
        except InvestigationBusy:
            pass
        else:
            raise AssertionError("second worker acquired live lease")
        assert repo.load_checkpoint(item.id) is None
        assert repo.get_incident(item.id).status == "open"
    asyncio.run(scenario())


def test_state_change_during_risk_check_blocks_write():
    async def scenario():
        repo = InMemoryDiagnosticRepository()
        plan = DiagnosticPlan(1, PlanTarget("runtime", "okx", "before"), "restart_exit_manager",
                              {}, ["e"], ["fresh"], "high", "healthy", ["healthy"])
        approvals = ApprovalService(repo)
        approvals.decide("incident", plan, "approve", "operator")
        fingerprint = "before"
        writes = []
        async def check(_):
            nonlocal fingerprint
            fingerprint = "changed"
            return True, "ok"
        async def execute(_):
            writes.append(1)
            return {}
        handler = ExecutionHandler(plan.action, check, lambda _: fingerprint, execute, lambda _: {"status": "satisfied"})
        result = await ControlledExecutor(repo, approvals, [handler]).execute("incident", plan)
        assert result.status == "stale_approval"
        assert not writes
    asyncio.run(scenario())


def test_restart_rejects_local_exposure_even_when_rest_is_empty():
    async def scenario():
        control = RuntimeControl(None, None, {}, asyncio.Event(), None, None, None)
        async def fresh():
            return {"positions": [], "orders": [], "local": [{"contracts": 1}]}
        control.fresh = fresh
        plan = SimpleNamespace(target=PlanTarget("runtime", "okx"), action="restart_exit_manager", parameters={},
                               evidence_ids=["observed"], preconditions=["empty"], verification=["healthy"], expected_result="healthy")
        assert (await control.safe(plan))[0] is False
        plan.target = PlanTarget("runtime", "other-account")
        assert (await control.safe(plan))[0] is False
    asyncio.run(scenario())


def test_expired_worker_cannot_overwrite_sqlite_checkpoint(tmp_path):
    lab = Sandbox(tmp_path / "lease.db")
    key = lab.initialize()
    repo = lab.repository
    assert repo.claim_lease(key, "old", -1)
    assert repo.claim_lease(key, "new", 30)
    repo.save_checkpoint(key, {"status": "investigating", "step": 2}, "new")
    try:
        repo.save_checkpoint(key, {"status": "failed", "step": 1}, "old")
    except InvestigationBusy:
        pass
    else:
        raise AssertionError("lost lease allowed a stale write")
    assert repo.load_checkpoint(key)["step"] == 2
    lab.engine.dispose()


def test_reopen_archives_investigation_and_invalidates_old_approval(tmp_path):
    async def scenario():
        lab = Sandbox(tmp_path / "reopen.db")
        key = lab.initialize()
        old = await lab.investigate()
        lab.review("approve", "operator")
        lab.repository.reopen(key, {"information": "verified externally", "reviewer": "operator"})
        new = await lab.investigate()
        assert new.plan.version > old.plan.version
        assert len(lab.repository.investigations_for(key)) == 1
        assert (await lab.execute()).status == "approval_required"
        lab.engine.dispose()
    asyncio.run(scenario())


def test_bad_agent_configuration_does_not_break_startup(monkeypatch):
    from diagnostics.bootstrap import build_service
    from diagnostics.service import configure_service
    monkeypatch.setenv("DIAGNOSTIC_AGENT_ENABLED", "1")
    monkeypatch.setenv("DIAGNOSTIC_MAX_STEPS", "invalid")
    service = build_service(SimpleNamespace())
    assert not service.enabled
    assert service.agent is None
    configure_service(None)


def test_unknown_execution_escalates_at_verification_deadline(tmp_path):
    async def scenario():
        lab = Sandbox(tmp_path / "deadline.db")
        key = lab.initialize()
        await lab.investigate()
        lab.review("approve", "operator")
        writes = []
        async def apply(_):
            writes.append(1)
            raise TimeoutError("response lost")
        handler = ExecutionHandler("sync_local_order", lambda _: (True, "ok"), lab.fingerprint,
                                   apply, lambda _: {"status": "unknown"})
        executor = ControlledExecutor(lab.repository, ApprovalService(lab.repository), [handler], verification_window_seconds=0)
        assert (await executor.execute(key, lab.plan())).status == "pending_confirmation"
        assert (await executor.execute(key, lab.plan())).status == "manual_review"
        assert writes == [1]
        lab.engine.dispose()
    asyncio.run(scenario())


def test_monitor_dispatches_only_approved_plan_once(tmp_path):
    from diagnostics.monitor import DiagnosticMonitor
    from diagnostics.service import IncidentService
    async def scenario():
        lab = Sandbox(tmp_path / "dispatch.db")
        lab.initialize()
        await lab.investigate()
        writes = []
        async def apply(_):
            writes.append(1)
            return {"requested": True}
        handler = ExecutionHandler("sync_local_order", lambda _: (True, "allowed"), lab.fingerprint,
                                   apply, lambda _: {"status": "satisfied"})
        executor = ControlledExecutor(lab.repository, ApprovalService(lab.repository), [handler])
        monitor = DiagnosticMonitor(IncidentService(lab.repository), SimpleNamespace(executor=lambda _: executor))
        await monitor.execute_approved()
        assert writes == []
        lab.review("approve", "operator")
        await monitor.execute_approved()
        await monitor.execute_approved()
        assert writes == [1]
        assert lab.incident().status == "resolved"
        lab.engine.dispose()
    asyncio.run(scenario())


def test_database_outage_spools_and_replays_without_blocking_workflow(tmp_path):
    from diagnostics.service import IncidentService
    async def scenario():
        class Repository(InMemoryDiagnosticRepository):
            blocking_io = True
            available = False
            def report(self, value):
                time.sleep(.08)
                if not self.available:
                    raise OSError("database unavailable")
                return super().report(value)
        repo = Repository()
        service = IncidentService(repo, spool_path=tmp_path / "spool")
        shutdown = asyncio.Event()
        worker = asyncio.create_task(service.ingest(shutdown))
        ticks = 0
        async def workflow():
            nonlocal ticks
            for _ in range(5):
                ticks += 1
                await asyncio.sleep(.005)
        service.submit_nowait(IncidentInput("durable", "unknown", "order", "1", utcnow()))
        await asyncio.gather(service.incoming.join(), workflow())
        assert ticks == 5
        assert list((tmp_path / "spool").glob("*.json"))
        repo.available = True
        await service.replay_spool()
        assert len(repo.list_incidents()) == 1
        assert not list((tmp_path / "spool").glob("*.json"))
        shutdown.set()
        await worker
    asyncio.run(scenario())
