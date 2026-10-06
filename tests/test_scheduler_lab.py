import pytest

from resource_advisor.scheduler_lab import LabReport, LabRequest, SchedulerLab
from resource_advisor.service import NotFound, Rejected
from resource_advisor.store import Conflict, Store


def test_native_experiment_scope_idempotency_and_cancellation():
    store = Store("sqlite://")
    store.initialize()
    lab = SchedulerLab(store)
    request = LabRequest(scenario="gang")
    with pytest.raises(Rejected):
        lab.start("team-a", request, "one")
    lab.heartbeat({"scenarios": ["gang", "topology"]})
    first = lab.start("team-a", request, "one")
    assert lab.start("team-a", request, "one")["ref"] == first["ref"]
    with pytest.raises(Conflict):
        lab.start("team-a", LabRequest(scenario="topology"), "one")
    with pytest.raises(Conflict):
        lab.start("team-b", request, "two")
    assert lab.listing("team-b")["items"] == []
    with pytest.raises(NotFound):
        lab.cancel("team-b", first["ref"])
    lab.cancel("team-a", first["ref"])
    report = LabReport(
        state="RUNNING", snapshot={"native_id": "real-123"}, event={"title": "observed"}
    )
    assert lab.report(first["ref"], report)["state"] == "CANCEL_REQUESTED"
    lab.report(first["ref"], LabReport(state="CANCELED"))
    assert lab.report(first["ref"], report)["state"] == "CANCELED"
    assert lab.start("team-b", request, "two")["state"] == "REQUESTED"


def test_multi_gpu_request_capacity_and_idempotency():
    from pydantic import ValidationError

    for value in (
        {"scenario": "multi_gpu"},
        {"scenario": "multi_gpu", "gpu_count": 0},
        {"scenario": "gang", "gpu_count": 3},
    ):
        with pytest.raises(ValidationError):
            LabRequest(**value)
    store = Store("sqlite://")
    store.initialize()
    lab = SchedulerLab(store)
    lab.heartbeat({"scenarios": ["multi_gpu"], "multi_gpu": {"max_gpus": 3}})
    with pytest.raises(Rejected):
        lab.start("team-a", LabRequest(scenario="multi_gpu", gpu_count=4), "oversized")
    request = LabRequest(scenario="multi_gpu", gpu_count=3)
    first = lab.start("team-a", request, "multi")
    assert first["body"]["gpu_count"] == 3
    assert lab.start("team-a", request, "multi")["ref"] == first["ref"]
    with pytest.raises(Conflict):
        lab.start("team-a", LabRequest(scenario="multi_gpu", gpu_count=2), "multi")


@pytest.mark.parametrize("scenario", ["heterogeneous", "npu", "mixed"])
def test_registered_workload_poc_uses_runner_project_only(scenario):
    store = Store("sqlite://")
    store.initialize()
    lab = SchedulerLab(store)
    lab.heartbeat({"scenarios": [scenario], "project_ref": "team-a"})
    with pytest.raises(Rejected):
        lab.start("team-b", LabRequest(scenario=scenario), "other-project")
    assert lab.start("team-a", LabRequest(scenario=scenario), "own-project")["state"] == "REQUESTED"
