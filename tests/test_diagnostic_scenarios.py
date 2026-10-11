from diagnostics.evaluation import run_offline_evaluation
from diagnostics.benchmark import SCENARIOS


def test_required_fault_scenarios_have_process_and_outcome_assertions():
    report = run_offline_evaluation()
    names = {item["name"] for item in report["cases"]}
    assert names == set(SCENARIOS)
    assert report["summary"]["case_count"] == 15
    assert report["summary"]["passed"] == 15, report
    assert report["summary"]["wrong_action_count"] == 0
    assert report["summary"]["duplicate_action_count"] == 0
    assert all(item["tool_policy_correct"] and item["business_state_correct"] for item in report["cases"])
