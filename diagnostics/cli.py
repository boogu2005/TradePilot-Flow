from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from database.db import get_session, init_db

from .approval import ApprovalService
from .domain import DiagnosticPlan, PlanTarget
from .repository import DiagnosticRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and review diagnostic incidents")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show")
    show.add_argument("incident_id")
    review = sub.add_parser("review")
    review.add_argument("incident_id")
    review.add_argument("--decision", required=True, choices=["approve", "modify", "reject", "request_information"])
    review.add_argument("--reviewer", required=True)
    review.add_argument("--expires-minutes", type=int, default=15)
    review.add_argument("--parameters-json")
    review.add_argument("--note")
    return parser


def _plan(checkpoint: dict | None) -> DiagnosticPlan:
    raw = checkpoint.get("plan") if checkpoint else None
    if not raw:
        raise SystemExit("incident has no reviewable plan")
    return DiagnosticPlan(
        version=raw["version"], target=PlanTarget(**raw["target"]), action=raw["action"],
        parameters=raw["parameters"], evidence_ids=raw["evidence_ids"],
        preconditions=raw["preconditions"], risk=raw["risk"],
        expected_result=raw["expected_result"], verification=raw["verification"],
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    init_db()
    repo = DiagnosticRepository(get_session)
    output: Any
    if args.command == "list":
        output = [asdict(item) for item in repo.list_incidents(args.limit)]
    elif args.command == "show":
        output = {"incident": asdict(repo.get_incident(args.incident_id)), "checkpoint": repo.load_checkpoint(args.incident_id),
                  "approvals": [asdict(item) for item in repo.approvals_for(args.incident_id)]}
    else:
        plan = _plan(repo.load_checkpoint(args.incident_id))
        modified = json.loads(args.parameters_json) if args.parameters_json else None
        approval = ApprovalService(repo).decide(
            args.incident_id, plan, args.decision, args.reviewer,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=args.expires_minutes),
            modified_parameters=modified, note=args.note,
        )
        output = asdict(approval)
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
