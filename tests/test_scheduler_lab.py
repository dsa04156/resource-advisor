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


@pytest.mark.parametrize(
    "scenario", ["heterogeneous", "npu", "mixed", "mixed_batch", "fleet_batch"]
)
def test_registered_workload_poc_uses_runner_project_only(scenario):
    store = Store("sqlite://")
    store.initialize()
    lab = SchedulerLab(store)
    lab.heartbeat({"scenarios": [scenario], "project_ref": "team-a"})
    with pytest.raises(Rejected):
        lab.start("team-b", LabRequest(scenario=scenario), "other-project")
    assert lab.start("team-a", LabRequest(scenario=scenario), "own-project")["state"] == "REQUESTED"


def test_node_history_is_project_scoped_frozen_and_samples_resource_changes(monkeypatch):
    from datetime import timedelta

    from resource_advisor.contracts import now
    from resource_advisor.inventory import save_inventory

    clock = now()
    monkeypatch.setattr("resource_advisor.scheduler_lab.now", lambda: clock)
    store = Store("sqlite://")
    store.initialize()
    lab = SchedulerLab(store)
    lab.heartbeat({"scenarios": ["gang"]})
    ref = lab.start("team-a", LabRequest(scenario="gang"), "history")["ref"]

    def inventory(ref, cpu, collected=None):
        stamp = (collected or clock).isoformat()
        return {
            "ref": ref,
            "cluster_ref": "cluster-a",
            "collected_at": stamp,
            "stale_after_seconds": 120,
            "status": "ok",
            "nodes": [
                {
                    "node_ref": "node-a",
                    "resources": {},
                    "telemetry": {
                        "cpu_usage_cores": {
                            "value": cpu,
                            "source": "metrics",
                            "observed_at": stamp,
                            "status": "ok",
                        }
                    },
                }
            ],
            "queues": [{"private": "not needed in node history"}],
        }

    save_inventory(store, "team-a", inventory("inventory-1", 1))
    save_inventory(store, "team-b", inventory("inventory-other-team", 99))
    report = LabReport(state="RUNNING", snapshot={"jobs": []}, event={"title": "running"})
    lab.report(ref, report)
    first = lab.listing("team-a")["items"][0]["body"]["events"][0]
    saved = first["snapshot"]["resource_observation"]
    assert saved["observed_at"] == first["observed_at"]
    assert len(saved["inventory"]) == 1
    assert "queues" not in saved["inventory"][0]
    sample = saved["inventory"][0]["nodes"][0]["telemetry"]["cpu_usage_cores"]
    assert sample["value"] == 1 and sample["status"] == "ok"

    # Node changes also get a replay sample when native job state is unchanged.
    clock += timedelta(seconds=16)
    save_inventory(store, "team-a", inventory("inventory-2", 2))
    lab.report(ref, LabReport(state="RUNNING", snapshot={"jobs": []}))
    assert lab.listing("team-a")["agent"]["seen_at"] == clock.isoformat()
    body = lab.listing("team-a")["items"][0]["body"]
    assert len(body["events"]) == 2
    assert body["events"][1]["title"] == "노드 자원 관측 갱신"
    assert body["events"][0]["snapshot"]["resource_observation"] == saved

    # The next read cannot age a historical sample or backfill it from current data.
    clock += timedelta(minutes=5)
    lab.report(ref, LabReport(state="RUNNING", snapshot={}, event={"title": "later"}))
    assert lab.listing("team-a")["agent"]["seen_at"] == clock.isoformat()
    body = lab.listing("team-a")["items"][0]["body"]
    stale = body["snapshot"]["resource_observation"]["inventory"][0]
    assert stale["status"] == "stale"
    assert stale["nodes"][0]["telemetry"]["cpu_usage_cores"]["value"] is None
    assert body["events"][0]["snapshot"]["resource_observation"] == saved


def test_missing_inventory_stays_missing_in_node_history():
    store = Store("sqlite://")
    store.initialize()
    lab = SchedulerLab(store)
    lab.heartbeat({"scenarios": ["gang"]})
    ref = lab.start("team-a", LabRequest(scenario="gang"), "missing")["ref"]
    lab.report(ref, LabReport(state="RUNNING", event={"title": "started"}))
    observation = lab.listing("team-a")["items"][0]["body"]["snapshot"]["resource_observation"]
    assert observation["status"] == "missing"
    assert observation["inventory"] == []


def test_history_listing_keeps_only_latest_replay_and_detail_is_project_scoped():
    store = Store("sqlite://")
    store.initialize()
    lab = SchedulerLab(store)
    lab.heartbeat({"scenarios": ["gang"]})
    old = lab.start("team-a", LabRequest(scenario="gang"), "older")
    lab.report(
        old["ref"],
        LabReport(state="SUCCEEDED", snapshot={"result": "retained"}, event={"title": "done"}),
    )
    current = lab.start("team-a", LabRequest(scenario="gang"), "newer")
    rows = lab.listing("team-a")["items"]
    assert rows[0]["ref"] == current["ref"] and not rows[0]["summary_only"]
    assert rows[1]["summary_only"] and "events" not in rows[1]["body"]
    detail = lab.get("team-a", old["ref"])
    assert detail["body"]["snapshot"]["result"] == "retained"
    assert detail["body"]["events"][0]["title"] == "done"
    with pytest.raises(NotFound):
        lab.get("team-b", old["ref"])
