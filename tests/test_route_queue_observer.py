from datetime import timedelta

import pytest

from resource_advisor.backends import BackendError
from resource_advisor.contracts import now
from resource_advisor.recommendation import RouteQueueSnapshot
from resource_advisor.route_queue_observer import QueueObserverConfig, observe, run_tick

CONFIG = {
    "project_ref": "team-a",
    "routes": [
        {
            "cluster_ref": "lab-vt-rtx5080-v1",
            "resource_key": "nvidia.com/gpu",
            "capacity_unit": "physical_device",
            "nodes": [{"node_ref": "gpu-b", "capacity": 1}],
        },
        {
            "cluster_ref": "lab-vt-orin9-v1",
            "resource_key": "nvidia.com/gpu.shared",
            "capacity_unit": "logical_slot",
            "nodes": [{"node_ref": "orin", "capacity": 2}],
        },
        {
            "cluster_ref": "lab-vt-ddp-amd64-v1",
            "resource_key": "nvidia.com/gpu",
            "capacity_unit": "physical_device",
            "nodes": [{"node_ref": "gpu-a", "capacity": 1}, {"node_ref": "gpu-b", "capacity": 1}],
        },
    ],
    "queue_targets": {
        "resource-advisor-lab/ra-vt-rtx5080": {
            "nodes": ["gpu-b"],
            "resource_keys": ["nvidia.com/gpu"],
        },
        "resource-advisor-lab/ra-vt-ddp-amd64": {
            "nodes": ["gpu-a", "gpu-b"],
            "resource_keys": ["nvidia.com/gpu"],
        },
        "resource-advisor-lab/ra-hailo": {"nodes": ["pi"], "resource_keys": ["hailo.ai/h8"]},
    },
}


def node(name, resources, ready="True", unschedulable=False):
    return {
        "metadata": {"name": name},
        "spec": {"unschedulable": True} if unschedulable else {},
        "status": {
            "capacity": dict(resources),
            "allocatable": dict(resources),
            "conditions": [{"type": "Ready", "status": ready}],
        },
    }


def pod(name, node_name, resource, *, ns="edge", job_id=None, phase="Running"):
    meta = {"name": name, "namespace": ns}
    if job_id:
        meta["annotations"] = {"resource-advisor/job-id": job_id}
    spec = {"containers": [{"resources": {"requests": {resource: "1"}}}]}
    if node_name:
        spec["nodeName"] = node_name
    return {
        "metadata": meta,
        "spec": spec,
        "status": {"phase": phase, "startTime": now().isoformat()},
    }


def queue(ns, name, pending=0):
    return {"metadata": {"namespace": ns, "name": name}, "status": {"pendingWorkloads": pending}}


NODES = [
    node("gpu-a", {"nvidia.com/gpu": "1"}),
    node("gpu-b", {"nvidia.com/gpu": "1"}),
    node("orin", {"nvidia.com/gpu.shared": "2"}),
]


class Fake:
    def __init__(self, pods=(), nodes=NODES, queues=(), fail=None):
        self.items = {"pods": list(pods), "nodes": list(nodes), "localqueues": list(queues)}
        self.fail = fail

    def list(self, kind):
        if kind == self.fail:
            raise BackendError("kubectl exited with code 1")
        return self.items[kind]


def by_route(snapshots):
    return {(s.cluster_ref, s.route_ref): s for s in snapshots}


def config():
    return QueueObserverConfig.model_validate(CONFIG)


def test_observed_idle_routes_are_complete_and_zero_per_member_node():
    stamp = now()
    snaps = by_route(observe(config(), Fake(), observed_at=stamp))
    # The DDP route is observed per member node under its own cluster identity.
    assert set(snaps) == {
        ("lab-vt-rtx5080-v1", "gpu-b"),
        ("lab-vt-orin9-v1", "orin"),
        ("lab-vt-ddp-amd64-v1", "gpu-a"),
        ("lab-vt-ddp-amd64-v1", "gpu-b"),
    }
    for s in snaps.values():
        assert s.complete and not s.running and not s.pending
        assert s.observed_at == stamp and s.source == "kubernetes:pods"
    orin = snaps[("lab-vt-orin9-v1", "orin")]
    assert (orin.capacity, orin.capacity_unit) == (2, "logical_slot")
    assert snaps[("lab-vt-ddp-amd64-v1", "gpu-a")].capacity_unit == "physical_device"


def test_external_occupant_on_a_shared_member_fails_closed_on_every_route_using_it():
    pods = [pod("qual-ddp-r1-0", "gpu-b", "nvidia.com/gpu", ns="ra-vision-training")]
    snaps = by_route(observe(config(), Fake(pods), observed_at=now()))
    for key in [("lab-vt-rtx5080-v1", "gpu-b"), ("lab-vt-ddp-amd64-v1", "gpu-b")]:
        s = snaps[key]
        assert not s.complete
        assert s.incomplete_reasons == (
            "OCCUPANCY_UNKNOWN:external:ra-vision-training/qual-ddp-r1-0",
        )
        assert [i.expected_occupancy_seconds for i in s.running] == [None]
    assert snaps[("lab-vt-ddp-amd64-v1", "gpu-a")].complete


def test_frozen_project_occupancy_makes_an_own_job_known():
    pods = [pod("ra-1", "gpu-b", "nvidia.com/gpu", ns="resource-advisor-lab", job_id="job-1")]
    snaps = by_route(
        observe(config(), Fake(pods), observed_at=now(), expected_occupancy={"job-1": 40.0})
    )
    s = snaps[("lab-vt-rtx5080-v1", "gpu-b")]
    assert s.complete and [(i.job_id, i.expected_occupancy_seconds) for i in s.running] == [
        ("job-1", 40.0)
    ]


@pytest.mark.parametrize(
    "nodes,reason",
    [
        ([n for n in NODES if n["metadata"]["name"] != "gpu-b"], "NODE_ABSENT:gpu-b"),
        (
            [node("gpu-b", {"nvidia.com/gpu": "1"}, ready="Unknown")] + NODES[::2],
            "NODE_NOT_READY:gpu-b",
        ),
        (
            [node("gpu-b", {"nvidia.com/gpu": "1"}, unschedulable=True)] + NODES[::2],
            "NODE_CORDONED:gpu-b",
        ),
        ([node("gpu-b", {"nvidia.com/gpu": "2"})] + NODES[::2], "CAPACITY_MISMATCH:gpu-b:2/1"),
        ([node("gpu-b", {})] + NODES[::2], "CAPACITY_MISMATCH:gpu-b:0/1"),
    ],
)
def test_unverified_node_capacity_is_never_reported_as_free(nodes, reason):
    snaps = by_route(observe(config(), Fake(nodes=nodes), observed_at=now()))
    for key in [("lab-vt-rtx5080-v1", "gpu-b"), ("lab-vt-ddp-amd64-v1", "gpu-b")]:
        s = snaps[key]
        assert not s.complete and reason in s.incomplete_reasons
        # Capacity stays the configured physical count, never the observed surplus.
        assert s.capacity == 1


def test_pending_kueue_demand_reaches_only_mapped_nodes_and_resources():
    queues = [
        queue("resource-advisor-lab", "ra-vt-ddp-amd64", pending=1),
        queue("resource-advisor-lab", "ra-hailo", pending=3),
    ]
    snaps = by_route(observe(config(), Fake(queues=queues), observed_at=now()))
    for key in [("lab-vt-ddp-amd64-v1", "gpu-a"), ("lab-vt-rtx5080-v1", "gpu-b")]:
        s = snaps[key]
        assert not s.complete
        assert s.incomplete_reasons == (
            "KUEUE_PENDING_UNKNOWN:resource-advisor-lab/ra-vt-ddp-amd64=1",
        )
        assert [i.job_id for i in s.pending] == ["kueue:resource-advisor-lab/ra-vt-ddp-amd64#0"]
    assert snaps[("lab-vt-orin9-v1", "orin")].complete


def test_pending_demand_in_an_unmapped_queue_fails_closed_everywhere():
    snaps = observe(
        config(), Fake(queues=[queue("mlops", "training", pending=2)]), observed_at=now()
    )
    for s in snaps:
        assert not s.complete
        assert s.incomplete_reasons == ("KUEUE_PENDING_UNMAPPED:mlops/training=2",)
        assert len(s.pending) == 2


@pytest.mark.parametrize("fail", ["pods", "nodes", "localqueues"])
def test_failed_observation_is_fresh_incomplete_and_carries_no_old_occupants(fail):
    stamp = now()
    snaps = observe(
        config(), Fake([pod("x", "gpu-b", "nvidia.com/gpu")], fail=fail), observed_at=stamp
    )
    assert len(snaps) == 4
    for s in snaps:
        assert s.observed_at == stamp and not s.complete
        assert s.incomplete_reasons == ("OBSERVATION_FAILED:" + fail,)
        assert not s.running and not s.pending


def test_ticks_store_fresh_immutable_snapshots(service):
    first = run_tick(service, config(), Fake(), at=now() - timedelta(seconds=15))
    second = run_tick(service, config(), Fake(), at=now())
    with service.store.transaction() as conn:
        rows = [
            RouteQueueSnapshot.model_validate(r["body"])
            for r in service.store.list(conn, "route_queue_snapshot", "team-a")
        ]
    assert len(rows) == 8 and len({r.ref for r in rows}) == 8
    assert {r.ref for r in rows} == {s.ref for s in first + second}
    assert min(s.observed_at for s in second) > max(s.observed_at for s in first)


def test_config_rejects_duplicate_route_members_and_slow_ticks():
    bad = {
        **CONFIG,
        "routes": [{**CONFIG["routes"][2], "nodes": [{"node_ref": "gpu-a", "capacity": 1}] * 2}],
    }
    with pytest.raises(ValueError):
        QueueObserverConfig.model_validate(bad)
    with pytest.raises(ValueError):
        QueueObserverConfig.model_validate({**CONFIG, "interval_seconds": 120})
