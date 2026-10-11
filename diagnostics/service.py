from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from .domain import Evidence, IncidentInput
from .storage import repository_call


class IncidentService:
    def __init__(self, repository, agent=None, *, enabled: bool = False, queue_size: int = 100, spool_path=None):
        self.repository = repository
        self.agent = agent
        self.enabled = enabled
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self._queued: set[str] = set()
        self.spool_path = Path(spool_path) if spool_path else None
        self.incoming: asyncio.Queue = asyncio.Queue(maxsize=queue_size)

    def submit_nowait(self, value):
        try:
            self.incoming.put_nowait(value)
        except asyncio.QueueFull:
            logger.error("[Diagnostics] ingress queue full; alert was not accepted")

    def _spool(self, value):
        if not self.spool_path:
            return None
        self.spool_path.mkdir(parents=True, exist_ok=True)
        from .redaction import redact
        path = self.spool_path / f"{uuid.uuid4().hex}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(redact(asdict(value)), default=str), encoding="utf-8")
        temporary.replace(path)
        return path

    async def ingest(self, shutdown_event):
        while not shutdown_event.is_set() or not self.incoming.empty():
            try:
                value = await asyncio.wait_for(self.incoming.get(), timeout=.5)
            except asyncio.TimeoutError:
                await self.replay_spool()
                continue
            try:
                path = await asyncio.to_thread(self._spool, value)
                item = await repository_call(self.repository, "report", value)
                if path:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                if self.enabled and item.status in ("open", "investigating") and item.id not in self._queued and not self.queue.full():
                    self.queue.put_nowait(item.id)
                    self._queued.add(item.id)
            except Exception as exc:  # noqa: BLE001 - preserve spool and trading liveness
                logger.error("[Diagnostics] ingest failed type={}", type(exc).__name__)
            finally:
                self.incoming.task_done()

    async def replay_spool(self):
        if not self.spool_path:
            return
        spool_path = self.spool_path
        paths = await asyncio.to_thread(lambda: list(spool_path.glob("*.json"))[:100])
        for path in paths:
            try:
                raw = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
                raw["occurred_at"] = datetime.fromisoformat(raw["occurred_at"])
                raw["evidence"] = [Evidence.from_dict(value) for value in raw.get("evidence", [])]
                await repository_call(self.repository, "report", IncidentInput(**raw))
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except Exception:  # noqa: BLE001 - retry durable spool after storage recovers
                return

    def report_nowait(self, value: IncidentInput):
        try:
            incident = self.repository.report(value)
        except Exception:  # noqa: BLE001 - diagnostic storage cannot break trading
            logger.error("[Diagnostics] persistence unavailable; incident not persisted")
            return None
        if not self.enabled or incident.status not in ("open", "investigating"):
            return incident
        if incident.id not in self._queued:
            try:
                self.queue.put_nowait(incident.id)
                self._queued.add(incident.id)
            except asyncio.QueueFull:
                logger.error("[Diagnostics] queue full; incident remains persisted")
        return incident

    def enqueue_resumable(self) -> int:
        if not self.enabled:
            return 0
        count = 0
        for incident_id in self.repository.resumable_ids():
            if incident_id not in self._queued and not self.queue.full():
                self.queue.put_nowait(incident_id)
                self._queued.add(incident_id)
                count += 1
        return count

    async def run(self, shutdown_event: asyncio.Event) -> None:
        ids = await repository_call(self.repository, "resumable_ids") if self.enabled else []
        for item_id in ids:
            if item_id not in self._queued and not self.queue.full():
                self.queue.put_nowait(item_id)
                self._queued.add(item_id)
        while not shutdown_event.is_set():
            try:
                incident_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Query off-thread; asyncio.Queue mutations remain on its owner loop.
                ids = await repository_call(self.repository, "resumable_ids") if self.enabled else []
                for item_id in ids:
                    if item_id not in self._queued and not self.queue.full():
                        self.queue.put_nowait(item_id)
                        self._queued.add(item_id)
                continue
            try:
                if self.agent:
                    state = await self.agent.run(incident_id)
                    await repository_call(self.repository, "set_status", incident_id, state.status)
            except Exception as exc:  # noqa: BLE001 - isolate one diagnostic task
                from .agent import InvestigationBusy
                if isinstance(exc, InvestigationBusy):
                    continue
                logger.error(f"[Diagnostics] incident={incident_id} failed: {type(exc).__name__}")
                try:
                    await repository_call(self.repository, "set_status", incident_id, "failed")
                except Exception:  # noqa: BLE001 - report unavailable failure storage
                    logger.error("[Diagnostics] could not persist failure status")
            finally:
                self._queued.discard(incident_id)
                self.queue.task_done()


_service: IncidentService | None = None


def configure_service(service: IncidentService | None) -> None:
    global _service
    _service = service


def report_nowait(value: IncidentInput):
    if _service and _service.spool_path:
        return _service.submit_nowait(value)
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
