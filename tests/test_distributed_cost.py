"""Distributed (DDP) queue + network JCT predictions through the public API.

Every member route needs its own fresh complete queue snapshot and destination
network measurement; the gang waits for its slowest member and the staging
critical path covers every member. Anything missing stays unknown (fail closed).
Evidence here is software fixtures on the contracts, not native runs.
"""

import hashlib
from datetime import timedelta

import pytest
import test_distributed_training as dt
import test_heterogeneous_recommendation as hetero
from fastapi.testclient import TestClient
from test_network_recommendation import DIGEST, TOKENS, binding, h, network

from resource_advisor.api import create_app
from resource_advisor.contracts import CapabilitySnapshot, RuntimeVariant, now, signature
from resource_advisor.recommendation import (
    HardwareFingerprint,
    QueueItem,
    jct_profile_from_jobs,
    member_rows,
    network_profile_from_receipts,
)

URL = "/api/v1/compute"
QUEUE = {"policy": "profile_queue"}
NETWORK = {"version": "hetero-rec-v2", "policy": "profile_queue_network"}


@pytest.fixture
def client(service, bundle):
    spec = dt.setup(service, bundle)
    dt.inventory(service, {"node-a": 1, "node-b": 1})
    location = hetero.location(spec, list(dt.NODES)).model_dump(mode="json")
    location.update(size_bytes=1_000_000, content_digest=DIGEST)
    keyed = {hashlib.sha256(k.encode()).hexdigest(): v for k, v in TOKENS.items()}
    c = TestClient(create_app(service, keyed))
    c.spec = spec
    post(c, "/data-locations", location)
    return c


def post(client, path, model):
    body = model if isinstance(model, dict) else model.model_dump(mode="json")
    response = client.post(URL + path, headers=h("operator"), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def ddp_profile(service, client, node="node-b", **change):
    """A DDP profile whose rank 0 ran on ``node`` (not necessarily the anchor)."""
    with service.store.transaction() as conn:
        cap = CapabilitySnapshot.model_validate(service.store.get(conn, "capability", "cap-" + node)["body"])
        variant = RuntimeVariant.model_validate(service.store.get(conn, "variant", "ddp-variant")["body"])
    # fmt: skip
    return hetero.jct_profile(client.spec, variant, cap, "ddp", 10.0, **change)


def queue(node, *, running=()):
    return hetero.queue_snapshot("q-" + node, node, running=running)


def busy(seconds_left):
    started = now() - timedelta(seconds=2)
    return (QueueItem(job_id="other", expected_occupancy_seconds=2 + seconds_left, started_at=started),)


def prediction(client, policy):
    post(client, "/scheduling-profiles", hetero.scheduling_profile("p", policy))
    response = client.post(URL + "/scheduling-plans", headers=h("user"),
                           json={"profile_ref": "p", "workload_ref": "ddp-workload"})  # fmt: skip
    assert response.status_code == 200, response.text
    result = response.json()
    evidence = (result.get("plan") or {}).get("recommendation_evidence") or result[
        "recommendation_evidence"
    ]
    [p] = [p for p in evidence["predictions"] if p["candidate_ref"] == "ddp"]
    return p, evidence, result


def test_gang_waits_for_the_slowest_member_route(service, client):
    post(client, "/jct-profiles", ddp_profile(service, client))
    post(client, "/route-queue-snapshots", queue("node-a"))
    post(client, "/route-queue-snapshots", queue("node-b", running=busy(30.0)))

    p, evidence, result = prediction(client, QUEUE)

    assert result["accepted"] is True
    assert p["queue_wait_seconds"] == pytest.approx(30.0, abs=1.0)  # node-b, not the anchor
    assert p["predicted_jct_seconds"] == pytest.approx(p["queue_wait_seconds"] + 1.0 + 13.0)
    assert "DISTRIBUTED_GANG_SLOWEST_MEMBER_WAIT" in p["reasons"]
    members = evidence["queue_snapshots"]["ddp"]["members"]
    assert [m["node_ref"] for m in members] == ["node-a", "node-b"]
    assert members[0]["queue_wait_seconds"] == 0.0


def test_a_member_without_queue_evidence_keeps_the_gang_unknown(service, client):
    post(client, "/jct-profiles", ddp_profile(service, client))
    post(client, "/route-queue-snapshots", queue("node-b"))

    p, _, result = prediction(client, QUEUE)

    assert p["predicted_jct_seconds"] is None
    assert p["reasons"] == ["QUEUE_MISSING", "DISTRIBUTED_MEMBER:node-a"]
    assert result["accepted"] is False


def network_evidence(client, seconds, groups=None):
    for node, value in seconds.items():
        extra = {"staging_group": groups[node]} if groups else {}
        post(client, "/network-profiles", network(node, value, workload_ref="ddp-workload",
                                                  dataset_version=client.spec.identity.dataset_version,
                                                  **extra))  # fmt: skip
        post(client, "/cache-bindings", binding(node))


SYNC = "ddp:j-1/a-1@node-a+node-b"


@pytest.mark.parametrize(
    ("groups", "aggregation", "staging"),
    [
        ({"node-a": SYNC, "node-b": SYNC}, "synchronized_ddp_parallel_max", 6.0),
        # another attempt's member set, or one member solo: no proven concurrency
        ({"node-a": SYNC, "node-b": "ddp:j-2/a-2@node-a+node-b"}, "shared_source_serialized_bound", 10.0),
        ({"node-a": "ddp:j-1/a-1@node-a+node-c", "node-b": "ddp:j-1/a-1@node-a+node-c"},
         "shared_source_serialized_bound", 10.0),
    ],
)  # fmt: skip
def test_same_native_ddp_attempt_staging_uses_the_observed_parallel_max(
    service, client, groups, aggregation, staging
):
    post(client, "/jct-profiles", ddp_profile(service, client))
    for node in dt.NODES:
        post(client, "/route-queue-snapshots", queue(node))
    network_evidence(client, {"node-a": [4.0], "node-b": [6.0]}, groups)

    p, _, _ = prediction(client, NETWORK)

    assert p["breakdown"]["staging"]["aggregation"] == aggregation
    assert p["breakdown"]["staging"]["seconds"] == staging
    assert p["predicted_jct_seconds"] == pytest.approx(1.0 + 2.0 + staging + 10.0)


def test_member_staging_critical_path_replaces_profile_staging_once(service, client):
    post(client, "/jct-profiles", ddp_profile(service, client))
    for node in dt.NODES:
        post(client, "/route-queue-snapshots", queue(node))
    network_evidence(client, {"node-a": [4.0], "node-b": [6.0]})

    p, evidence, result = prediction(client, NETWORK)

    staging = p["breakdown"]["staging"]
    # One shared source: solo measurements cannot show contention, so serialized bound.
    assert staging["aggregation"] == "shared_source_serialized_bound"
    assert staging["seconds"] == 10.0 and staging["parallel_lower_bound_seconds"] == 6.0
    assert sorted(n["node_ref"] for n in staging["per_node"]) == ["node-a", "node-b"]
    # admission 1 + startup 2 + member staging 10 + compute 10 (NCCL inside compute, once)
    assert p["predicted_jct_seconds"] == pytest.approx(23.0)
    assert dict(p["phase_seconds"])["data_staging"] == 10.0
    record = result["plan"]["recommendation"]
    assert sorted(record["cache_bindings"]["ddp"]) == ["node-a", "node-b"]
    assert sorted(n["ref"] for n in record["network_profiles"]["ddp"]) == [
        "net-node-a-cold",
        "net-node-b-cold",
    ]


def test_missing_member_measurement_or_inseparable_staging_stays_unknown(service, client):
    post(client, "/jct-profiles", ddp_profile(service, client))
    for node in dt.NODES:
        post(client, "/route-queue-snapshots", queue(node))
    network_evidence(client, {"node-b": [6.0]})

    p, _, _ = prediction(client, NETWORK)
    assert p["predicted_jct_seconds"] is None
    assert p["reasons"] == ["NETWORK_PROFILE_MISSING", "DISTRIBUTED_MEMBER:node-a"]

    inside = ddp_profile(service, client, ref="jct-ddp-inside", staging_boundary="inside_compute",
                         phases={"runtime_startup": 2.0, "compute": 11.0},
                         measured_at=now() - timedelta(minutes=1))  # fmt: skip
    post(client, "/jct-profiles", inside)
    p, _, _ = prediction(client, NETWORK)
    assert p["reasons"] == ["STAGING_NOT_SEPARABLE"]  # never subtracted from compute


def test_profile_from_a_member_with_another_runtime_is_not_used(service, client):
    other = ddp_profile(service, client, environment_digest="sha256:" + "9" * 64)
    post(client, "/jct-profiles", other)
    for node in dt.NODES:
        post(client, "/route-queue-snapshots", queue(node))

    p, _, _ = prediction(client, QUEUE)
    assert p["reasons"] == ["PROFILE_RUNTIME_MISMATCH"]


def staged_ddp_row(members=None):
    """A finished staged DDP job: rank 0 on node-b, rank 1 on node-a (adapter-observed)."""
    base = now() - timedelta(minutes=5)
    result = {"evidence_kind": "hardware", "outcome": "COMPLETED", "attempt_id": "a-1",
              "measurements": {"elapsed_seconds": 10.0, "peak_memory_mib": 100.0}}  # fmt: skip
    ranks = [{"index": 0, "pod_uid": "uid-0", "node_name": "node-b", "phase": "Succeeded"},
             {"index": 1, "pod_uid": "uid-1", "node_name": "node-a", "phase": "Succeeded"}]  # fmt: skip

    def receipt(rank, seconds):
        return {
            "job_id": "j-1", "attempt_id": "a-1", "content_digest": DIGEST, "size_bytes": 1_000_000,
            "source_endpoint_ref": "storage", "source_kind": "http",
            "source_locator": "http://storage.invalid/dataset-v1.tar", "observer_host": "pod",
            "identity_source": "kubernetes_downward_api", "node_name": rank["node_name"],
            "pod_uid": rank["pod_uid"], "cache_state": "cold", "cache_locator": "/var/cache/ra",
            "cache_storage_identity": "11:22", "staged_at": (base + timedelta(seconds=6)).isoformat(),
            "sample": {"elapsed_seconds": seconds, "bytes": 1_000_000, "content_verified": True},
        }  # fmt: skip

    staged = [receipt(ranks[0], 6.0), receipt(ranks[1], 4.0)]
    body = {
        "spec": {"ref": "ddp-workload", "project_ref": "team-a",
                 "identity": {"dataset_version": "v1", "precision": "fp32", "batch_size": 4,
                              "input_shape": [4, 3, 224, 224]}},
        "candidate": {"ref": "ddp", "backend": "kubernetes"},
        "capability": {"backend_cluster_id": "lab", "resource_key": "nvidia.com/gpu",
                       "environment_digest": DIGEST, "runtime_versions": {}},
        "workload_signature": DIGEST, "context_signature": DIGEST, "attempt_id": "a-1",
        "quality_passed": True, "result_digest": signature(result),
        "placement": {"source": "kubernetes:Pod", "pod_uid": "uid-0", "node_name": "node-b",
                      "distributed": {"world_size": 2, "ranks": ranks}},
        "scheduler_submitted_at": base.isoformat(),
        "started_at": (base + timedelta(seconds=2)).isoformat(),
        "execution_started_at": (base + timedelta(seconds=5)).isoformat(),
        "backend_finished_at": (base + timedelta(seconds=25)).isoformat(),
    }  # fmt: skip
    return {"id": "j-1", "state": "SUCCEEDED", "body": body, "result": result,
            "staging": staged[0], "staging_members": staged if members is None else members}  # fmt: skip


HARDWARE = HardwareFingerprint(accelerator_vendor="nvidia", accelerator_model="fixture-gpu",
                               device_class="gpu", memory_model="discrete",
                               allocation_mode="physical_device", logical_slot=False)  # fmt: skip


def test_ddp_profile_staging_is_the_slowest_members_own_receipt():
    made = jct_profile_from_jobs([staged_ddp_row()], ref="ddp", valid_for_seconds=600,
                                 hardware=HARDWARE)  # fmt: skip
    assert made.staging_boundary == "separate"
    # 20 s in container = compute 10 + slowest member staging 6 + residual 4 (never twice)
    assert made.phases == {"runtime_startup": 3.0, "compute": 10.0,
                           "unattributed_in_container": 4.0, "data_staging": 6.0}  # fmt: skip

    [a, b] = sorted(member_rows(staged_ddp_row()), key=lambda r: r["body"]["placement"]["node_name"])
    net = network_profile_from_receipts([a], ref="net-a", valid_for_seconds=3600)
    assert (net.node_ref, net.samples[0].elapsed_seconds, net.native_job_ids) == ("node-a", 4.0, ("j-1",))
    assert net.staging_group == "ddp:j-1/a-1@node-a+node-b"  # concurrent with node-b, barriers
    solo = {**a, "member_of": None}
    assert network_profile_from_receipts([solo], ref="s", valid_for_seconds=3600).staging_group is None


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m[:1],  # a rank without its own receipt
        lambda m: [m[0], {**m[1], "pod_uid": "uid-replaced"}],  # not the observed rank Pod
        lambda m: [m[0], {**m[1], "node_name": "node-b"}],  # Pod claims another node
        lambda m: [m[0], {**m[1], "content_digest": "sha256:" + "cd" * 32}],  # other dataset
    ],
)
def test_ddp_staging_needs_every_observed_ranks_own_receipt(change):
    row = staged_ddp_row()
    row["staging_members"] = change(row["staging_members"])
    with pytest.raises(ValueError, match="rank|dataset"):
        jct_profile_from_jobs([row], ref="ddp", valid_for_seconds=600, hardware=HARDWARE)


def test_collector_cli_reads_every_stored_rank_receipt(service):
    import sys
    from pathlib import Path

    from sqlalchemy import insert

    from resource_advisor.store import jobs

    sys.path.insert(0, str(Path(__file__).parents[1] / "examples"))
    import hetero_collect

    row = staged_ddp_row()
    with service.store.transaction() as conn:
        conn.execute(insert(jobs).values(id="j-1", project="team-a", idempotency_key="k",
                                         request_digest=None, state="SUCCEEDED", epoch=1,
                                         version=1, body=row["body"]))  # fmt: skip
        service.store.put(conn, "result", "a-1", "team-a", row["result"])
        service.store.put(conn, "staging_receipt", "a-1", "team-a", row["staging"])
        for index, receipt in enumerate(row["staging_members"]):
            service.store.put(conn, "staging_member_receipt", f"a-1:{index}", "team-a", receipt)
        stored = hetero_collect.stored_rows(service.store, conn, "j-1")
        assert stored["staging_members"] == row["staging_members"]
        # An extra stored receipt for this attempt is loaded too, and then refused.
        service.store.put(conn, "staging_member_receipt", "a-1:2", "team-a",
                          {**row["staging_members"][1], "pod_uid": "uid-extra"})  # fmt: skip
        extra = hetero_collect.stored_rows(service.store, conn, "j-1")
    assert len(extra["staging_members"]) == 3
    with pytest.raises(ValueError, match="every observed rank"):
        member_rows(extra)
    [only] = hetero_collect.network_rows([stored], "node-a")
    made = network_profile_from_receipts([only], ref="net-a", valid_for_seconds=3600)
    assert made.node_ref == "node-a" and made.identity_source == "ra_job_receipts"


def test_route_queue_lookup_is_the_exact_routes_latest_row(service):
    from sqlalchemy import text

    from resource_advisor.store import latest_route_queue_query

    at = now()

    def put(ref, *, project="team-a", seconds=0, resource_key="nvidia.com/gpu", route="node-a"):
        body = hetero.queue_snapshot(ref, route, observed_at=at + timedelta(seconds=seconds))
        body = body.model_copy(update={"project_ref": project, "resource_key": resource_key})
        with service.store.transaction() as conn:
            service.store.put(conn, "route_queue_snapshot", ref, project, body.model_dump(mode="json"))

    put("old", seconds=-30)
    put("tie-a", seconds=0)
    put("tie-b", seconds=0)  # same observation time: the larger ref wins, as before
    put("other-project", project="team-b", seconds=60)
    put("other-resource", resource_key="vendor/gpu.shared", seconds=60)
    put("other-route", route="node-b", seconds=60)
    put("no-key", resource_key=None, seconds=90)

    def latest(resource_key="nvidia.com/gpu"):
        with service.store.transaction() as conn:
            row = service.store.latest_route_queue(conn, "team-a", "kubernetes", "lab", "node-a",
                                                   resource_key)  # fmt: skip
        return row and row["ref"]

    assert latest() == "tie-b"
    assert latest(None) == "no-key"
    assert latest("missing") is None
    query = latest_route_queue_query(("team-a", "kubernetes", "lab", "node-a", "nvidia.com/gpu"))
    with service.store.transaction() as conn:
        if conn.dialect.name == "sqlite":  # bounded seek, no scan or sort of history
            sql = str(query.compile(conn, compile_kwargs={"literal_binds": True}))
            plan = " ".join(r[-1] for r in conn.execute(text("EXPLAIN QUERY PLAN " + sql)))
            assert "USING INDEX ix_ra_route_queue_latest" in plan and "TEMP B-TREE" not in plan
        else:
            names = conn.execute(text("SELECT indexname FROM pg_indexes")).scalars().all()
            assert "ix_ra_route_queue_latest" in names


def test_native_timestamp_resolution_is_reported_apart_from_n1_spread(service, client):
    from resource_advisor.recommendation import timestamp_resolution

    assert timestamp_resolution("2026-10-29T12:00:01Z", "2026-10-29T12:00:09Z") == 1.0
    assert timestamp_resolution("2026-10-29T12:00:01.250Z", "2026-10-29T12:00:09.1+00:00") == 0.1

    row = staged_ddp_row()
    for key in ("scheduler_submitted_at", "started_at", "execution_started_at", "backend_finished_at"):
        row["body"][key] = row["body"][key].split(".")[0] + "+00:00"  # Kubernetes: whole seconds
    made = jct_profile_from_jobs([row], ref="ddp-1", valid_for_seconds=600, hardware=HARDWARE)
    assert made.timestamp_resolution_seconds == 1.0 and made.repeats == 1

    post(client, "/jct-profiles", ddp_profile(service, client, repeats=1, jct_stddev_seconds=None,
                                              timestamp_resolution_seconds=1.0))  # fmt: skip
    for node in dt.NODES:
        post(client, "/route-queue-snapshots", queue(node))
    p, _, _ = prediction(client, QUEUE)
    uncertainty = p["breakdown"]["uncertainty"]
    assert uncertainty["native_timestamp_resolution_seconds"] == 1.0
    assert uncertainty["statistical_spread"] == "unknown (n=1)"
    assert "profile_spread" in uncertainty["missing"]


def test_measured_unpack_and_preparation_leave_only_true_residue_unattributed():
    row = staged_ddp_row()
    for m in row["staging_members"]:
        m["unpack_seconds"] = 0.5 if m["pod_uid"] == "uid-0" else 1.5
    row["staging"] = {**row["staging_members"][0], "preparation_seconds": 2.0}
    made = jct_profile_from_jobs([row], ref="ddp", valid_for_seconds=600, hardware=HARDWARE)
    # critical member (rank 0, 6 s transfer) unpack 0.5 + rank-0 preparation 2.0, once
    assert made.phases == {"runtime_startup": 3.0, "compute": 10.0, "data_staging": 6.0,
                           "model_preparation": 2.5, "unattributed_in_container": 1.5}  # fmt: skip

    plain = staged_ddp_row()
    plain["id"] = "j-2"
    for m in (*plain["staging_members"], plain["staging"]):
        m["job_id"] = "j-2"
    with pytest.raises(ValueError, match="preparation"):
        jct_profile_from_jobs([row, plain], ref="mixed", valid_for_seconds=600, hardware=HARDWARE)
