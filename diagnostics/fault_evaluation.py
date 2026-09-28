"""Execute safety fault cases and derive measurements from pytest's JUnit output."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def evaluate() -> dict:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="diagnostic-evaluation-") as directory:
        report = Path(directory) / "results.xml"
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "tests/test_diagnostic_review_regressions.py", f"--junitxml={report}"],
            cwd=root, capture_output=True, text=True, timeout=120, check=False,
        )
        if not report.exists():
            raise RuntimeError(f"fault evaluation did not produce a report: {completed.returncode}")
        cases = []
        for item in ET.parse(report).getroot().iter("testcase"):
            cases.append({"name": item.get("name"), "latency_seconds": float(item.get("time", "0")),
                          "passed": item.find("failure") is None and item.find("error") is None and item.find("skipped") is None})
    return {"kind": "executed_safety_fault_tests", "n": len(cases),
            "passed": sum(case["passed"] for case in cases), "exit_code": completed.returncode,
            "cases": cases, "model_tokens": None, "baseline_comparison": None,
            "limitations": "No external model calls. Latency is test execution time. This measures safety assertions, not model effectiveness or a trading error rate."}


if __name__ == "__main__":
    result = evaluate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(result["exit_code"])
