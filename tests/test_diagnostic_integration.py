import asyncio

from diagnostics.repository import InMemoryDiagnosticRepository
from diagnostics.service import (
    IncidentService,
    configure_service,
    report_protection_repair_exhausted,
)


class TradeStub:
    id = 42
    pair = "BTC/USDT:USDT"
    position_state = "open"
    repair_retry = 3
    sl_algo_id = "sl-1"
    tp1_algo_id = "tp-1"


def test_exhausted_repair_emits_persistent_deduplicated_incident():
    async def scenario():
        repo = InMemoryDiagnosticRepository()
        service = IncidentService(repo, enabled=True)
        configure_service(service)
        try:
            one = report_protection_repair_exhausted(TradeStub(), "create_sl", "timeout")
            two = report_protection_repair_exhausted(TradeStub(), "create_sl", "timeout")
            return one, two, repo
        finally:
            configure_service(None)

    one, two, repo = asyncio.run(scenario())
    assert one.id == two.id
    assert repo.get_incident(one.id).occurrences == 2
    assert repo.get_incident(one.id).recovery_steps[-1] == "repair_retry_3"


def test_diagnostic_worker_restores_checkpointed_task():
    async def scenario():
        repo = InMemoryDiagnosticRepository()
        service = IncidentService(repo, agent=None, enabled=True)
        incident = report = service.repository.report(__import__("diagnostics.domain", fromlist=["IncidentInput"]).IncidentInput(
            "order:resume", "unknown", "order", "resume",
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ))
        repo.save_checkpoint(incident.id, {"step": 1})
        return service.enqueue_resumable(), await service.queue.get()

    count, incident_id = asyncio.run(scenario())
    assert count == 1
    assert incident_id
