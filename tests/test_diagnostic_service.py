import asyncio
from datetime import datetime, timezone

from diagnostics.domain import Evidence, IncidentInput
from diagnostics.repository import InMemoryDiagnosticRepository
from diagnostics.service import IncidentService


def test_disabled_service_does_not_enqueue_or_block_workflow():
    service = IncidentService(InMemoryDiagnosticRepository(), enabled=False)
    result = service.report_nowait(IncidentInput(
        correlation_key="trade:1", event_type="repair_failed", object_type="trade",
        object_id="1", occurred_at=datetime.now(timezone.utc),
    ))
    assert result is not None
    assert service.queue.qsize() == 0


def test_duplicate_events_share_one_incident_and_one_queue_slot():
    async def scenario():
        repo = InMemoryDiagnosticRepository()
        service = IncidentService(repo, enabled=True)
        value = IncidentInput(
            correlation_key="trade:1", event_type="repair_failed", object_type="trade",
            object_id="1", occurred_at=datetime.now(timezone.utc),
            evidence=[Evidence("workflow", "failure", {"retry": 3})],
        )
        one = service.report_nowait(value)
        two = service.report_nowait(value)
        return one, two, service.queue.qsize()

    one, two, size = asyncio.run(scenario())
    assert one.id == two.id
    assert two.occurrences == 2
    assert size == 1
