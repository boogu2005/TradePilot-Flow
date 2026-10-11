from diagnostics.demo import run_demo


def test_demo_runs_full_incident_to_verified_execution():
    report = run_demo()
    assert report["stages"] == [
        "incident_persisted", "evidence_queried", "plan_ready",
        "human_approved", "controlled_execution", "business_verified",
    ]
    assert report["agent"]["status"] == "waiting_human"
    assert report["execution"]["status"] == "verified"
    assert report["audit"]["tool_calls"] == 2
