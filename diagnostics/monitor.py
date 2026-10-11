"""Rule monitoring and dispatch of explicitly approved recovery plans."""
import asyncio
import os
import time
from dataclasses import asdict

from loguru import logger

from .domain import AgentState, Evidence, IncidentInput, utcnow
from .storage import repository_call
from .telemetry import observations, observe


class DiagnosticMonitor:
    def __init__(self, service, control, interval=30):
        self.service, self.control, self.interval = service, control, interval
        self.started = time.monotonic()
        self.executor = control.executor(service.repository)

    async def execute_approved(self):
        if os.getenv("DIAGNOSTIC_EXECUTION_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
            return
        incidents = await repository_call(self.service.repository, "list_incidents", 200)
        for incident in incidents:
            if incident.status not in ("waiting_human", "waiting_approval", "waiting_external_state"):
                continue
            checkpoint = await repository_call(self.service.repository, "load_checkpoint", incident.id)
            if not checkpoint or not checkpoint.get("plan"):
                continue
            state = AgentState.from_checkpoint(incident, checkpoint)
            if state.plan and state.plan.action in self.executor.handlers:
                approvals = await repository_call(self.service.repository, "approvals_for", incident.id)
                if any(item.decision == "approve" for item in approvals):
                    result = await self.executor.execute(incident.id, state.plan)
                    if result.status == "pending_confirmation":
                        await repository_call(self.service.repository, "set_status", incident.id, "waiting_external_state")

    async def sample(self):
        now = time.time()
        for name, key in (("consumer", "consumer_last_tick"), ("exit_manager", "monitor_last_tick")):
            tick = self.control.heartbeat.get(key, 0)
            status = "UNKNOWN" if not tick else "NORMAL" if now - tick < 120 else "STALE"
            observe(name, status, heartbeat_age=now - tick if tick else None)
        for name, status in self.control.tasks.stats().items():
            if name.startswith("诊断") or name == "异常诊断Agent":
                continue
            observe(f"worker:{name}", "NORMAL" if status == "running" else "FAILED", state=status)
        health = self.control.runtime.get_health()
        state = health.get("ws", {}).get("state", "NOT_STARTED")
        observe("websocket", "NORMAL" if state == "RUNNING" else "FAILED" if state == "FAILED" else "DEGRADED", state=state)
        try:
            await self.control.reconcile_readonly()
            observe("database", "NORMAL")
        except Exception as exc:  # noqa: BLE001 - monitoring failure must not stop trading
            from sqlalchemy.exc import SQLAlchemyError
            observe("database" if isinstance(exc, SQLAlchemyError) else "rest", "UNKNOWN", error_type=type(exc).__name__)
        if time.monotonic() - self.started < 90:
            return
        for observation in list(observations.values()):
            if observation.status == "NORMAL" or observation.failures < 3:
                continue
            self.service.submit_nowait(IncidentInput(
                f"monitor:{observation.source}", f"{observation.source.split(':')[0]}_unresolved",
                "runtime", "okx", utcnow(), ["startup_grace_elapsed", "three_unhealthy_observations"],
                [Evidence("rule_monitor", observation.status, asdict(observation))],
            ))

    async def run(self, shutdown):
        while not shutdown.is_set():
            try:
                await self.sample()
                await self.execute_approved()
            except Exception as exc:  # noqa: BLE001 - monitoring isolation
                logger.error("[Diagnostics] monitor error type={}", type(exc).__name__)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue
