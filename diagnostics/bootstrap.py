from __future__ import annotations

import os
from pathlib import Path

from database.db import get_session
from .adapters import build_read_only_tools
from .agent import AgentBudgets, DiagnosticAgent
from .model import OpenAICompatibleDiagnosticModel
from .repository import DiagnosticRepository
from .service import IncidentService, configure_service


def build_service(parser) -> IncidentService | None:
    enabled = os.getenv("DIAGNOSTIC_AGENT_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    if not enabled:
        configure_service(None)
        return None
    repository = DiagnosticRepository(get_session)
    root = Path(__file__).resolve().parents[1]
    tools = build_read_only_tools(
        get_session,
        Path(os.getenv("DIAGNOSTIC_LOG_PATH", root / "user_data" / "logs" / "systemd.log")),
        [root / "docs" / "diagnostics" / "runbook.md"],
    )
    budgets = AgentBudgets(
        max_steps=int(os.getenv("DIAGNOSTIC_MAX_STEPS", "7")),
        max_seconds=float(os.getenv("DIAGNOSTIC_MAX_SECONDS", "60")),
        max_tool_calls=int(os.getenv("DIAGNOSTIC_MAX_TOOL_CALLS", "10")),
        max_tokens=int(os.getenv("DIAGNOSTIC_MAX_TOKENS", "8000")),
        max_no_progress=int(os.getenv("DIAGNOSTIC_MAX_NO_PROGRESS", "3")),
    )
    model = OpenAICompatibleDiagnosticModel(
        parser.client,
        os.getenv("DIAGNOSTIC_AGENT_MODEL", parser.model),
        max_tokens=min(1600, budgets.max_tokens),
    )
    service = IncidentService(repository, DiagnosticAgent(repository, tools, model, budgets), enabled=True)
    configure_service(service)
    return service
