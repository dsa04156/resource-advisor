import json
from datetime import timedelta

import pytest
from test_inventory import config, source

from resource_advisor.backends import BackendError
from resource_advisor.contracts import now
from resource_advisor.inventory import InventoryCollector, fresh_view
from resource_advisor.queue_inventory import queue_view, workload_view


def queue():
    return {
        "metadata": {"name": "allowed", "generation": 2},
        "spec": {
            "resourceGroups": [
                {
                    "flavors": [
                        {
                            "name": "shared",
                            "resources": [
                                {"name": "cpu", "nominalQuota": "2"},
                                {
                                    "name": "vendor/gpu.shared",
                                    "nominalQuota": "4",
                                    "borrowingLimit": "2",
                                },
                            ],
                        }
                    ]
                }
            ]
        },
        "status": {
            "pendingWorkloads": 1,
            "conditions": [{"type": "Active", "status": "True", "observedGeneration": 2}],
            "flavorsReservation": [
                {
                    "name": "shared",
                    "resources": [
                        {"name": "cpu", "total": "1500m", "borrowed": "0"},
                        {"name": "vendor/gpu.shared", "total": "5", "borrowed": "1"},
                    ],
                }
            ],
            "flavorsUsage": [
                {
                    "name": "shared",
                    "resources": [
                        {"name": "cpu", "total": "500m"},
                        {"name": "vendor/gpu.shared", "total": "3"},
                    ],
                }
            ],
        },
    }


def test_quota_reservation_admission_and_borrowing_are_distinct():
    view = queue_view(queue(), config().resource_types, cluster=True)
    assert view["scope"] == "cluster_queue_all_namespaces"
    cpu, gpu = view["resources"]
    assert (cpu["nominal_quota"], cpu["reserved"], cpu["admitted"]) == (2, 1.5, 0.5)
    assert (gpu["nominal_quota"], gpu["reserved"], gpu["admitted"]) == (4, 5, 3)
    assert gpu["borrowed_reservation"] == 1 and gpu["allocation_mode"] == "virtual_slot"
    assert view["admitted_workloads"] is None  # Missing is not zero.
    assert "headroom" not in json.dumps(view)


def test_missing_usage_is_unknown_and_stale_generation_not_active():
    raw = queue()
    raw["status"].pop("flavorsUsage")
    raw["status"]["conditions"][0]["observedGeneration"] = 1
    view = queue_view(raw, {}, cluster=True)
    assert view["active"] is None
    assert all(r["admitted"] is None for r in view["resources"])
    raw["status"]["flavorsReservation"][0]["resources"][0]["total"] = "NaN"
    assert queue_view(raw, {}, cluster=True)["resources"][0]["reserved"] is None


def test_workload_reports_quota_and_pending_reason_without_guessing_priority():
    raw = {
        "metadata": {"name": "pending", "creationTimestamp": now().isoformat()},
        "spec": {"queueName": "team"},
        "status": {
            "conditions": [
                {
                    "type": "QuotaReserved",
                    "status": "False",
                    "reason": "Pending",
                    "message": "insufficient quota",
                },
            ],
            "admissionChecks": [
                {"name": "images", "state": "Pending", "message": "checking runtime"}
            ],
        },
    }
    view = workload_view(raw)
    assert view["quota_reserved"] is False and view["admitted"] is None
    assert view["priority"] is None
    assert view["conditions"][0]["message"] == "insufficient quota"
    assert view["admission_checks"][0]["state"] == "Pending"
    raw["status"]["conditions"].append({"type": "Finished", "status": "True"})
    assert workload_view(raw) is None


@pytest.mark.parametrize("denied", [False, True])
def test_named_cluster_queue_read_is_scoped_and_freshness_expires(denied):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if "clusterqueues.kueue.x-k8s.io" in args:
            assert args[args.index("clusterqueues.kueue.x-k8s.io") + 1] == "allowed"
            if denied:
                raise BackendError("forbidden")
            return json.dumps(queue())
        return source()(args, **kwargs)

    snapshot = InventoryCollector(
        config(cluster_queue_refs=("allowed",)), execute=execute
    ).collect()
    observed = snapshot["cluster_queues"][0]["observation"]
    assert observed["status"] == ("unavailable" if denied else "ok")
    assert (observed["value"] is None) == denied
    expired = fresh_view(snapshot, at=now() + timedelta(hours=1))
    assert expired["cluster_queues"][0]["observation"]["value"] is None
    assert len([c for c in calls if "clusterqueues.kueue.x-k8s.io" in c]) == 1


def test_unconfigured_cluster_queue_is_never_read():
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return source()(args, **kwargs)

    snapshot = InventoryCollector(config(queue_namespaces=("team-a",)), execute=execute).collect()
    assert snapshot["cluster_queues"] == []
    assert not any("clusterqueues.kueue.x-k8s.io" in c for c in calls)
    local = next(c for c in calls if "localqueues.kueue.x-k8s.io" in c)
    assert local[local.index("-n") + 1] == "team-a"
