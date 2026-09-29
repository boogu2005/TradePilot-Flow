"""Reproducible fake-exchange evaluation; failed cases produce a nonzero exit."""
from __future__ import annotations

import json

from .benchmark import evaluate


def run_offline_evaluation() -> dict:
    return evaluate()


if __name__ == "__main__":
    report = run_offline_evaluation()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["summary"]["passed"] == report["n"] else 1)
