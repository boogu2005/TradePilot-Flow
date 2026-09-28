from diagnostics.evaluation import run_offline_evaluation


def test_required_fault_scenarios_have_process_and_outcome_assertions():
    report = run_offline_evaluation()
    names = {item["name"] for item in report["cases"]}
    assert names == {
        "timeout_but_filled", "partial_fill_local_lag", "ws_down_rest_available",
        "ws_quiet", "no_new_evidence", "position_changed_during_approval",
        "duplicate_event_approval_resume", "invalid_tool", "insufficient_evidence",
        "model_unavailable",
    }
    assert report["summary"]["case_count"] == 10
    assert report["summary"]["wrong_action_count"] == 0
    assert all("tool_calls" in item and "elapsed_ms" in item and "token_usage" in item for item in report["cases"])
