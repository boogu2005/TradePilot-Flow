from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from .domain import Evidence, IncidentInput


class IncidentService:
    def __init__(self, repository, agent=None, *, enabled: bool = False, queue_size: int = 100):
        self.repository = repository
        self.agent = agent
        self.enabled = enabled
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self._queued: set[str] = set()

    def report_nowait(self, value: IncidentInput):
        if not self.enabled:
            return None
        incident = self.repository.report(value)
        if incident.id not in self._queued:
            try:
                self.queue.put_nowait(incident.id)
                self._queued.add(incident.id)
            except asyncio.QueueFull:
                logger.error("[Diagnostics] queue full; incident remains persisted")
        return incident

    def enqueue_resumable(self) -> int:
        count = 0
        for incident_id in self.repository.resumable_ids():
            if incident_id not in self._queued and not self.queue.full():
                self.queue.put_nowait(incident_id)
                self._queued.add(incident_id)
                count += 1
        return count

    async def run(self, shutdown_event: asyncio.Event) -> None:
        self.enqueue_resumable()
        while not shutdown_event.is_set():
            try:
                incident_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                if self.agent:
                    self.repository.set_status(incident_id, "investigating")
                    state = await self.agent.run(incident_id)
                    self.repository.set_status(incident_id, state.status)
            except Exception as exc:
                logger.error(f"[Diagnostics] incident={incident_id} failed: {type(exc).__name__}: {exc}")
                self.repository.set_status(incident_id, "failed")
            finally:
                self._queued.discard(incident_id)
                self.queue.task_done()


_service: IncidentService | None = None


def configure_service(service: IncidentService | None) -> None:
    global _service
    _service = service


def report_nowait(value: IncidentInput):
    return _service.report_nowait(value) if _service else None


def current_service() -> IncidentService | None:
    return _service


def report_protection_repair_exhausted(trade, task_type: str, error: str):
    """Narrow workflow hook: persist and enqueue only after normal retries end."""
    return report_nowait(IncidentInput(
        correlation_key=f"protection:{trade.id}:{task_type}",
        event_type="protection_repair_exhausted",
        object_type="trade",
        object_id=str(trade.id),
        occurred_at=datetime.now(timezone.utc),
        recovery_steps=["rest_preflight", task_type, f"repair_retry_{trade.repair_retry}"],
        evidence=[Evidence("workflow", "repair_failure", {
            "trade_id": trade.id, "symbol": trade.pair,
            "position_state": trade.position_state, "task_type": task_type,
            "repair_retry": trade.repair_retry, "error": str(error)[:500],
            "sl_reference": getattr(trade, "sl_algo_id", None),
            "tp_reference": getattr(trade, "tp1_algo_id", None),
        })],
    ))
