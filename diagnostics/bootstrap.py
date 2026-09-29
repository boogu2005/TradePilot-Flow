from __future__ import annotations

import math
import os
from pathlib import Path

from loguru import logger

from database.db import get_session

from .adapters import build_read_only_tools
from .agent import AgentBudgets, DiagnosticAgent
from .model import OpenAICompatibleDiagnosticModel
from .repository import DiagnosticRepository
from .service import IncidentService, configure_service


def _build_service(parser) -> IncidentService:
    enabled = os.getenv("DIAGNOSTIC_AGENT_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    root = Path(__file__).resolve().parents[1]
    spool = root / "user_data" / "diagnostic_spool"
    if not enabled:
        service = IncidentService(DiagnosticRepository(get_session), enabled=False, spool_path=spool)
        configure_service(service)
        return service
    repository = DiagnosticRepository(get_session)
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
    if not (1 <= budgets.max_steps <= 50 and 1 <= budgets.max_tool_calls <= 100
            and 1 <= budgets.max_tokens <= 100000 and 1 <= budgets.max_no_progress <= 20
            and math.isfinite(budgets.max_seconds) and 1 <= budgets.max_seconds <= 600):
        raise ValueError("diagnostic budgets outside supported bounds")
    model = OpenAICompatibleDiagnosticModel(
        parser.client,
        os.getenv("DIAGNOSTIC_AGENT_MODEL", "").strip() or parser.model,
        max_tokens=min(1600, budgets.max_tokens),
    )
    service = IncidentService(repository, DiagnosticAgent(repository, tools, model, budgets), enabled=True, spool_path=spool)
    configure_service(service)
    return service


def build_service(parser) -> IncidentService:
    try:
        return _build_service(parser)
    except Exception as exc:  # noqa: BLE001 - optional investigator cannot stop trading startup
        logger.error("[Diagnostics] investigator disabled: configuration error type={}", type(exc).__name__)
        root = Path(__file__).resolve().parents[1]
        service = IncidentService(DiagnosticRepository(get_session), enabled=False,
                                  spool_path=root / "user_data" / "diagnostic_spool")
        configure_service(service)
        return service
