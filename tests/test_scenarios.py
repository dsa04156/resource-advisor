import pytest
from test_scheduling import profile

from resource_advisor.contracts import State
from resource_advisor.scenarios import QueueScenarioRequest, QueueScenarios
from resource_advisor.service import NotFound
from resource_advisor.store import Conflict


def test_scenario_retries_partial_submission_without_duplicate_job(service, monkeypatch):
    service.register("scheduling_profile", profile(), "team-a")
    scenarios = QueueScenarios(service)
    request = QueueScenarioRequest(workload_ref="workload-1", profile_ref="interactive-v1")
    submit = service.submit

    def interrupted(project, value, key):
        if key.endswith("-1"):
            raise RuntimeError("interrupted after first job")
        return submit(project, value, key)

    monkeypatch.setattr(service, "submit", interrupted)
    with pytest.raises(RuntimeError):
        scenarios.start("team-a", request, "retry-key")
    ref = scenarios.recent("team-a")["items"][0]["ref"]
    first = scenarios.view("team-a", ref)["jobs"][0]["job_id"]
    monkeypatch.setattr(service, "submit", submit)
    result = scenarios.start("team-a", request, "retry-key")
    assert result["jobs"][0]["job_id"] == first
    assert len({j["job_id"] for j in result["jobs"]}) == 2
    again = scenarios.start("team-a", request, "retry-key")
    assert [j["job_id"] for j in again["jobs"]] == [j["job_id"] for j in result["jobs"]]
    with pytest.raises(NotFound):
        scenarios.view("other-team", ref)
    with pytest.raises(Conflict):
        scenarios.start(
            "team-a", request.model_copy(update={"profile_ref": "changed"}), "retry-key"
        )


def test_transition_history_preserves_queue_reason_after_completion(service):
    service.register("scheduling_profile", profile(), "team-a")
    scenarios = QueueScenarios(service)
    result = scenarios.start(
        "team-a",
        QueueScenarioRequest(workload_ref="workload-1", profile_ref="interactive-v1"),
        "history",
    )
    job_id = result["jobs"][0]["job_id"]
    for state, reason in [
        (State.QUEUED, "AdmissionPending"),
        (State.QUEUED, "AdmissionPending"),
        (State.RUNNING, None),
        (State.SUCCEEDED, None),
    ]:
        with service.store.transaction() as conn:
            row = service.store.job(conn, job_id)
            service.store.change_job(conn, row, state, {**row["body"], "scheduler_reason": reason})
    events = scenarios.view("team-a", result["ref"])["jobs"][0]["lifecycle_events"]
    assert [(e["state"], e["reason"]) for e in events] == [
        ("QUEUED", "AdmissionPending"),
        ("RUNNING", None),
        ("SUCCEEDED", None),
    ]
    assert all(e["observed_at"] for e in events)
