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
