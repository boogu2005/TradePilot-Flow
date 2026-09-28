from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base
from diagnostics.domain import AgentState, Evidence, IncidentInput, ToolCallRecord, ToolResult
from diagnostics.repository import DiagnosticRepository


def make_repo(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'diagnostics.db'}")
    Base.metadata.create_all(engine)
    return DiagnosticRepository(sessionmaker(bind=engine, expire_on_commit=False))


def test_duplicate_incident_merges_evidence_and_increments_occurrences(tmp_path):
    repo = make_repo(tmp_path)
    first = repo.report(IncidentInput(
        correlation_key="protection:trade-42",
        event_type="protection_repair_exhausted",
        object_type="trade",
        object_id="42",
        occurred_at=datetime.now(timezone.utc),
        recovery_steps=["rest_query", "repair_retry_3"],
        evidence=[Evidence("local", "repair", {"retry": 3})],
    ))
    second = repo.report(IncidentInput(
        correlation_key="protection:trade-42",
        event_type="protection_repair_exhausted",
        object_type="trade",
        object_id="42",
        occurred_at=datetime.now(timezone.utc),
        recovery_steps=["rest_query"],
        evidence=[Evidence("exchange_rest", "position", {"contracts": "1"})],
    ))

    assert second.id == first.id
    assert second.occurrences == 2
    assert {item.source for item in second.evidence} == {"local", "exchange_rest"}


def test_checkpoint_round_trip_is_separate_from_business_state(tmp_path):
    repo = make_repo(tmp_path)
    incident = repo.report(IncidentInput(
        correlation_key="order:abc",
        event_type="order_result_unknown",
        object_type="order",
        object_id="abc",
        occurred_at=datetime.now(timezone.utc),
    ))
    repo.save_checkpoint(incident.id, {"step": 2, "status": "investigating", "evidence": []})

    assert repo.load_checkpoint(incident.id)["step"] == 2
    assert repo.get_incident(incident.id).status == "open"


def test_full_agent_checkpoint_is_json_serializable(tmp_path):
    repo = make_repo(tmp_path)
    incident = repo.report(IncidentInput(
        correlation_key="order:json", event_type="unknown", object_type="order",
        object_id="json", occurred_at=datetime.now(timezone.utc),
    ))
    state = AgentState(incident)
    state.tool_calls.append(ToolCallRecord(
        "query", {"id": "json"}, ToolResult.ok("exchange_rest", {"state": "filled"}),
        datetime.now(timezone.utc), 3,
    ))
    repo.save_checkpoint(incident.id, state.to_checkpoint())
    assert repo.load_checkpoint(incident.id)["tool_calls"][0]["result"]["observed_at"].endswith("+00:00")
