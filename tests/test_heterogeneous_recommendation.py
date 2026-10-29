"""Opt-in heterogeneous recommendation through public seams.

All profiles, queues and links below are software fixtures (synthetic=False only
to exercise the real-evidence path); none is a hardware measurement.
"""

import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import BackendError, KubernetesBackend
from resource_advisor.contracts import JobRequest, now, signature
from resource_advisor.policy import context_signature
from resource_advisor.recommendation import (
    DataLocation,
    HardwareFingerprint,
    JctProfile,
    NodeAddress,
    NodePool,
    QueueItem,
    RouteQueueSnapshot,
    jct_profile_from_jobs,
    queue_snapshot_from_pods,
)
from resource_advisor.scheduling import SchedulingPlanRequest, SchedulingProfile, compile_plan
from resource_advisor.service import Rejected


def scheduling_profile(ref="hetero-v1", recommendation=None, **policy):
    body = {
        "ref": ref,
        "project_ref": "team-a",
        "name": "Hetero",
        "version": 1,
        "policy": {
            "max_resources": {"host_cpu": 2, "host_memory_mib": 1024, "accelerator_count": 1},
            "max_run_seconds": 30,
            "max_queue_seconds": 90,
            **policy,
        },
        "bindings": [
            {
                "backend": "kubernetes",
                "cluster_ref": "lab",
                "namespace": "research-a",
                "local_queue": "batch",
                "priority_map": {"normal": None},
            }
        ],
    }
    if recommendation:
        body["policy"]["recommendation"] = recommendation
    return SchedulingProfile.model_validate(body)


@pytest.fixture
def pair(service, bundle):
    """Two verified candidates; legacy order prefers ``base`` by reference."""
    spec, candidate, variant, cap = bundle
    spare_cap = cap.model_copy(update={"ref": "spare-cap", "node_ref": "spare-node"})
    spare = candidate.model_copy(update={"ref": "spare", "capability_ref": spare_cap.ref})
    variant = variant.model_copy(update={"ref": "pair-variant", "workload_ref": "pair-work"})
    spec = spec.model_copy(
        update={
            "ref": "pair-work",
            "candidates": tuple(
                c.model_copy(update={"variant_ref": variant.ref}) for c in (candidate, spare)
            ),
        }
    )
    service.operational_mode = True
    for kind, model in [("capability", spare_cap), ("variant", variant), ("workload", spec)]:
        service.register(kind, model, "team-a")
    return spec, variant, {"base": cap, "spare": spare_cap}


def jct_profile(spec, variant, cap, candidate_ref, compute, **change):
    candidate = next(c for c in spec.candidates if c.ref == candidate_ref)
    body = {
        "ref": f"jct-{candidate_ref}-{compute}".replace(".", "-"),
        "project_ref": "team-a",
        "workload_ref": spec.ref,
        "candidate_ref": candidate_ref,
        "backend": "kubernetes",
        "cluster_ref": cap.backend_cluster_id,
        "node_ref": cap.node_ref,
        "resource_key": cap.resource_key,
        "workload_signature": signature(spec.identity),
        "context_signature": context_signature(candidate, variant),
        "environment_digest": cap.environment_digest,
        "runtime_versions": cap.runtime_versions,
        "hardware": HardwareFingerprint(
            accelerator_vendor=cap.accelerator_vendor,
            accelerator_model=cap.accelerator_model,
            device_class=cap.device_class,
            memory_model=cap.memory_model,
            allocation_mode=cap.allocation_mode,
            logical_slot=False,
        ),
        "precision": spec.identity.precision,
        "batch_size": spec.identity.batch_size,
        "input_shape": spec.identity.input_shape,
        "dataset_version": spec.identity.dataset_version,
        "measured_at": now() - timedelta(minutes=5),
        "valid_for_seconds": 3600,
        "repeats": 3,
        "admission_seconds": 1.0,
        "readmission_seconds": 1.0,
        "phases": {"runtime_startup": 2.0, "data_staging": 1.0, "compute": compute},
        "staging_boundary": "separate",
        "quality_passed": True,
        "synthetic": False,
        "job_ids": ("fixture-job",),
        "source": "operator_import",
    }
    body.update(change)
    return JctProfile.model_validate(body)


def preview(service, profile, workload_ref="pair-work", **request):
    with service.store.transaction() as conn:
        return compile_plan(
            service,
            conn,
            "team-a",
            SchedulingPlanRequest(profile_ref=profile.ref, workload_ref=workload_ref, **request),
        )


def register(service, *items):
    for kind, model in items:
        service.register(kind, model, "team-a")


def test_legacy_profile_signature_and_plan_are_unchanged_by_the_opt_in_fields(service, pair):
    legacy = scheduling_profile()
    assert "recommendation" not in legacy.model_dump(mode="json")["policy"]
    assert "target_mode" not in legacy.model_dump(mode="json")["policy"]
    register(service, ("scheduling_profile", legacy))
    plan = preview(service, legacy)["plan"]
    assert plan["candidate_ref"] == "base"
    assert plan["node_ref"] == "test-worker"
    assert "recommendation" not in plan and "target" not in plan


def test_profile_only_selects_the_measured_faster_candidate_and_links_the_native_job(service, pair):
    from test_worker import SchedulerDouble

    from resource_advisor.worker import Worker

    spec, variant, caps = pair
    profile = scheduling_profile(recommendation={"policy": "profile_only"})
    slow = jct_profile(spec, variant, caps["base"], "base", 9.0)
    fast = jct_profile(spec, variant, caps["spare"], "spare", 3.0)
    register(service, ("scheduling_profile", profile), ("jct_profile", slow), ("jct_profile", fast))
    request = JobRequest(workload_ref=spec.ref, scheduling_profile_ref=profile.ref)
    job = service.submit("team-a", request, "hetero-1")
    assert service.submit("team-a", request, "hetero-1")["job_id"] == job["job_id"]
    assert Worker(service, {("team-a", "lab"): SchedulerDouble()}).submit_one()
    link = service_linkage(service, job["job_id"])
    assert link["native_job_id"] == "42"
    assert link["candidate_ref"] == "spare"
    rec = link["recommendation"]
    assert rec["policy"] == "profile_only" and rec["version"] == "hetero-rec-v1"
    assert rec["status"] == "SELECTED" and rec["fallback"] is None
    assert rec["profiles"]["spare"]["ref"] == fast.ref
    jct = {
        p["candidate_ref"]: p["predicted_jct_seconds"]
        for p in link["recommendation_evidence"]["predictions"]
    }
    # Profile-only: startup 2 + staging 1 + compute; no queue or admission.
    assert jct == {"base": 12.0, "spare": 6.0}


def service_linkage(service, job_id):
    from resource_advisor.recommendation import decision_linkage

    return decision_linkage(service, "team-a", job_id)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"synthetic": True}, "PROFILE_SYNTHETIC"),
        ({"quality_passed": False}, "PROFILE_QUALITY_FAILED"),
        ({"runtime_versions": {"cuda": "other"}}, "PROFILE_RUNTIME_MISMATCH"),
        ({"node_ref": "elsewhere"}, "PROFILE_ROUTE_MISMATCH"),
        ({"precision": "int8"}, "PROFILE_WORKLOAD_MISMATCH"),
        ({"measured_at": now() - timedelta(hours=3)}, "PROFILE_STALE"),
        ({"peak_memory_mib": 4096.0}, "PROFILE_MEMORY_EXCEEDED"),
    ],
)
def test_unusable_profile_needs_profile_instead_of_ranking(service, pair, change, reason):
    spec, variant, caps = pair
    profile = scheduling_profile(recommendation={"policy": "profile_only"})
    register(
        service,
        ("scheduling_profile", profile),
        ("jct_profile", jct_profile(spec, variant, caps["base"], "base", 1.0, **change)),
        ("jct_profile", jct_profile(spec, variant, caps["spare"], "spare", 1.0, **change)),
    )
    result = preview(service, profile)
    assert not result["accepted"]
    assert result["reasons"] == ["NEEDS_PROFILE"]
    reasons = {
        p["candidate_ref"]: p["reasons"] for p in result["recommendation_evidence"]["predictions"]
    }
    assert reasons["base"] == [reason]


def test_hardware_fingerprint_keeps_shared_memory_and_logical_slots_distinct():
    with pytest.raises(ValueError, match="dedicated"):
        HardwareFingerprint(
            accelerator_vendor="nvidia",
            accelerator_model="orin",
            device_class="gpu",
            memory_model="unified",
            dedicated_memory_mib=8192,
            allocation_mode="physical_device",
            logical_slot=False,
        )
    with pytest.raises(ValueError, match="logical"):
        HardwareFingerprint(
            accelerator_vendor="nvidia",
            accelerator_model="rtx",
            device_class="gpu",
            memory_model="discrete",
            allocation_mode="virtual_slot",
            logical_slot=False,
        )


def test_preexisting_service_span_cannot_be_counted_again(service, pair):
    spec, variant, caps = pair
    with pytest.raises(ValueError, match="preexisting"):
        jct_profile(
            spec,
            variant,
            caps["base"],
            "base",
            1.0,
            phases={"model_preparation": 4.0, "compute": 1.0, "data_staging": 1.0},
            excluded_preexisting_phases=("model_preparation",),
        )


def test_missing_profile_uses_only_an_explicit_recorded_legacy_fallback(service, pair):
    strict = scheduling_profile(recommendation={"policy": "profile_only"})
    fallback = scheduling_profile(
        "hetero-fallback", recommendation={"policy": "profile_only", "fallback": "legacy"}
    )
    register(service, ("scheduling_profile", strict), ("scheduling_profile", fallback))
    assert preview(service, strict)["reasons"] == ["NEEDS_PROFILE"]
    plan = preview(service, fallback)["plan"]
    assert plan["candidate_ref"] == "base"
    assert plan["recommendation"]["status"] == "LEGACY_FALLBACK"
    assert plan["recommendation"]["fallback"]["reason"] == "NEEDS_PROFILE"


def queue_snapshot(ref, route, *, complete=True, running=(), observed_at=None):
    return RouteQueueSnapshot(
        ref=ref,
        project_ref="team-a",
        backend="kubernetes",
        cluster_ref="lab",
        route_ref=route,
        resource_key="nvidia.com/gpu",
        observed_at=observed_at or now(),
        complete=complete,
        incomplete_reasons=() if complete else ("OCCUPANCY_UNKNOWN:external",),
        capacity=1,
        running=running,
        source="fixture",
        evidence_ref="fixture-queue",
    )


def test_queue_aware_policy_avoids_a_busy_fast_device_and_abstains_on_incomplete_queues(
    service, pair
):
    spec, variant, caps = pair
    profile = scheduling_profile(recommendation={"policy": "profile_queue"})
    register(
        service,
        ("scheduling_profile", profile),
        ("jct_profile", jct_profile(spec, variant, caps["base"], "base", 2.0)),
        ("jct_profile", jct_profile(spec, variant, caps["spare"], "spare", 5.0)),
        ("route_queue_snapshot", queue_snapshot("q-spare", "spare-node", complete=False)),
    )
    blocked = preview(service, profile)
    assert blocked["reasons"] == ["ESTIMATE_UNAVAILABLE"]
    busy = QueueItem(job_id="other", expected_occupancy_seconds=100.0, started_at=now())
    register(
        service,
        ("route_queue_snapshot", queue_snapshot("q-base", "test-worker", running=(busy,))),
        ("route_queue_snapshot", queue_snapshot("q-spare-2", "spare-node")),
    )
    result = preview(service, profile)
    assert result["plan"]["candidate_ref"] == "spare"
    evidence = result["plan"]["recommendation_evidence"]
    assert evidence["queue_snapshots"]["spare"]["ref"] == "q-spare-2"


def test_accepted_plan_is_rechecked_and_drift_is_rejected(service, pair):
    spec, variant, caps = pair
    profile = scheduling_profile(recommendation={"policy": "profile_only"})
    register(
        service,
        ("scheduling_profile", profile),
        ("jct_profile", jct_profile(spec, variant, caps["base"], "base", 2.0)),
    )
    first = preview(service, profile, candidate_ref="base")
    # Newer evidence for the same candidate changes the frozen evidence set.
    register(service, ("jct_profile", jct_profile(spec, variant, caps["base"], "base", 1.0)))
    with pytest.raises(Rejected, match="SCHEDULING_PLAN_CHANGED"):
        service.submit(
            "team-a",
            JobRequest(
                workload_ref=spec.ref,
                candidate_ref="base",
                scheduling_profile_ref=profile.ref,
                scheduling_plan_digest=first["digest"],
            ),
            "drift",
        )


def test_recommendation_never_inherits_operational_staleness_relaxation(service, pair):
    spec, variant, caps = pair
    stale = caps["spare"].model_copy(
        update={"ref": "stale-cap", "observed_at": now() - timedelta(days=1)}
    )
    legacy = scheduling_profile()
    rec = scheduling_profile("hetero-stale", recommendation={"policy": "profile_only"})
    work = spec.model_copy(
        update={
            "ref": "stale-work",
            "candidates": (spec.candidates[1].model_copy(update={"capability_ref": stale.ref}),),
            "baseline_candidate_ref": "spare",
        }
    )
    variant2 = variant.model_copy(update={"ref": "stale-variant", "workload_ref": work.ref})
    work = work.model_copy(
        update={
            "candidates": (work.candidates[0].model_copy(update={"variant_ref": variant2.ref}),)
        }
    )
    register(
        service,
        ("capability", stale),
        ("variant", variant2),
        ("workload", work),
        ("scheduling_profile", legacy),
        ("scheduling_profile", rec),
    )
    assert preview(service, legacy, workload_ref=work.ref)["accepted"]
    assert "CAPABILITY_STALE" in preview(service, rec, workload_ref=work.ref)["excluded"]["spare"]


def topology(endpoints, links):
    from resource_advisor.placement import TopologySnapshot

    stamp = now() - timedelta(seconds=10)
    return TopologySnapshot.model_validate(
        {
            "ref": "topo-1",
            "project_ref": "team-a",
            "evidence_ref": "fixture-links",
            "measurement_method": "fixture",
            "observed_at": stamp,
            "expires_at": stamp + timedelta(hours=1),
            "endpoints": endpoints,
            "links": [
                {
                    "source_endpoint_ref": a,
                    "target_endpoint_ref": b,
                    "bandwidth_bytes_per_second": bw,
                    "request_overhead_ms": 0,
                    "measured_at": stamp,
                    "evidence_ref": "fixture-link",
                }
                for a, b, bw in links
            ],
        }
    )


def endpoint(ref, node):
    return {"endpoint_ref": ref, "backend": "kubernetes", "cluster_ref": "lab", "node_ref": node}


def location(spec, readers, ref="data-1"):
    return DataLocation(
        ref=ref,
        project_ref="team-a",
        workload_ref=spec.ref,
        dataset_version=spec.identity.dataset_version,
        size_bytes=1_000_000_000,
        source_endpoint_ref="storage",
        storage_kind="nfs",
        accessible_nodes=tuple(
            NodeAddress(backend="kubernetes", cluster_ref="lab", node_ref=n) for n in readers
        ),
        provenance_ref="fixture-provenance",
        observed_at=now(),
    )


def test_network_policy_uses_measured_links_and_abstains_without_data_location(service, pair):
    spec, variant, caps = pair
    profile = scheduling_profile(
        recommendation={"policy": "profile_queue_network", "topology_snapshot_ref": "topo-1"}
    )
    snapshot = topology(
        [
            endpoint("storage", "nas"),
            endpoint("base-ep", "test-worker"),
            endpoint("spare-ep", "spare-node"),
        ],
        # 1 GB at 1 GB/s = 1 s to base; at 0.1 GB/s = 10 s to spare.
        [("storage", "base-ep", 1e9), ("storage", "spare-ep", 1e8)],
    )
    register(
        service,
        ("scheduling_profile", profile),
        ("topology_snapshot", snapshot),
        ("jct_profile", jct_profile(spec, variant, caps["base"], "base", 4.0)),
        ("jct_profile", jct_profile(spec, variant, caps["spare"], "spare", 2.0)),
        ("route_queue_snapshot", queue_snapshot("q-base", "test-worker")),
        ("route_queue_snapshot", queue_snapshot("q-spare", "spare-node")),
    )
    assert preview(service, profile)["reasons"] == ["ESTIMATE_UNAVAILABLE"]
    register(service, ("data_location", location(spec, ["test-worker", "spare-node"])))
    plan = preview(service, profile)["plan"]
    # base: 1 + 2 + 1 + 4 = 8; spare: 1 + 2 + 10 + 2 = 15 (staging replaced, not added).
    assert plan["candidate_ref"] == "base"
    jct = {
        p["candidate_ref"]: p["predicted_jct_seconds"]
        for p in plan["recommendation_evidence"]["predictions"]
    }
    assert jct == {"base": 8.0, "spare": 15.0}
    assert plan["recommendation"]["data_location"]["ref"] == "data-1"


def test_data_access_is_a_hard_filter(service, pair):
    spec, _, _ = pair
    profile = scheduling_profile(recommendation={"policy": "profile_only", "fallback": "legacy"})
    register(
        service,
        ("scheduling_profile", profile),
        ("data_location", location(spec, ["spare-node"])),
    )
    result = preview(service, profile)
    assert "DATA_ACCESS_DENIED" in result["excluded"]["base"]
    assert result["plan"]["candidate_ref"] == "spare"


def pool_fixture(service, pair, **pool):
    spec, variant, caps = pair
    pooled = NodePool.model_validate(
        {
            "ref": "gpu-pool",
            "project_ref": "team-a",
            "backend": "kubernetes",
            "cluster_ref": "lab",
            "member_capability_refs": ["cap-1", "spare-cap"],
            "kubernetes_node_selector": {"ra.example/qualified": "fixture-gpu"},
            "verification_evidence_ref": "fixture-pool-proof",
            **pool,
        }
    )
    profile = scheduling_profile("pool-v1", target_mode="pool")
    register(service, ("node_pool", pooled), ("scheduling_profile", profile))
    return profile


def test_pool_mode_restricts_native_choice_to_verified_members(service, pair):
    profile = pool_fixture(service, pair)
    result = preview(service, profile, candidate_ref="base")
    target = result["plan"]["target"]
    assert target["mode"] == "pool" and result["plan"]["node_ref"] is None
    assert {m["node_ref"] for m in target["members"]} == {"test-worker", "spare-node"}
    job = service.submit(
        "team-a",
        JobRequest(
            workload_ref="pair-work",
            candidate_ref="base",
            scheduling_profile_ref=profile.ref,
            scheduling_plan_digest=result["digest"],
        ),
        "pool-1",
    )
    with service.store.transaction() as conn:
        row = dict(service.store.job(conn, job["job_id"]))
    backend = KubernetesBackend(
        namespace="research-a", local_queue="batch", node_selector={"pool": "lab"}
    )
    pod = backend.manifest(row)["spec"]["template"]["spec"]
    assert "kubernetes.io/hostname" not in pod["nodeSelector"]
    assert pod["nodeSelector"]["ra.example/qualified"] == "fixture-gpu"
    term = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert term["nodeSelectorTerms"] == [
        {
            "matchExpressions": [
                {
                    "key": "kubernetes.io/hostname",
                    "operator": "In",
                    "values": ["spare-node", "test-worker"],
                }
            ]
        }
    ]
    pinned = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"kubernetes.io/hostname": "test-worker"},
    )
    with pytest.raises(BackendError, match="allowlist"):
        pinned.manifest(row)


def test_pool_with_an_incompatible_member_fails_closed(service, pair):
    _, _, caps = pair
    odd = caps["spare"].model_copy(
        update={"ref": "odd-cap", "node_ref": "odd", "runtime_versions": {"cuda": "x"}}
    )
    register(service, ("capability", odd))
    profile = pool_fixture(service, pair, member_capability_refs=["cap-1", "odd-cap"])
    result = preview(service, profile)
    assert "POOL_MEMBER_INCOMPATIBLE" in result["excluded"]["base"]
    # Undeclared pool membership is never inferred.
    assert "POOL_UNDECLARED" in result["excluded"]["spare"]


def test_slurm_pool_uses_only_a_verified_exclusive_feature_and_keeps_the_training_guard(
    service, pair
):
    from test_backends import native_runtime

    from resource_advisor.backends import SlurmBackend

    profile = pool_fixture(service, pair)
    plan = preview(service, profile, candidate_ref="base")
    job = service.submit(
        "team-a",
        JobRequest(
            workload_ref="pair-work",
            candidate_ref="base",
            scheduling_profile_ref=profile.ref,
            scheduling_plan_digest=plan["digest"],
        ),
        "slurm-shape",
    )
    with service.store.transaction() as conn:
        row = dict(service.store.job(conn, job["job_id"]))
    row["body"] = dict(row["body"])
    target = dict(row["body"]["scheduling_plan"]["target"])
    row["body"]["scheduling_plan"] = dict(
        row["body"]["scheduling_plan"],
        target=target,
        adapter={"partition": "compute", "account": "team-a", "qos": "normal"},
    )
    backend = SlurmBackend(
        partition="compute",
        account="team-a",
        qos="normal",
        output_dir="/tmp/ra-test",
        native_runtimes=native_runtime(row),
    )
    row["body"]["capability"] = dict(row["body"]["capability"], resource_key="gpu:test")
    with pytest.raises(BackendError, match="exclusive"):
        backend.script(row)
    target.update(slurm_constraint="ra_q1", slurm_constraint_exclusive_evidence_ref="proof")
    script = backend.script(row)
    assert "#SBATCH --constraint=ra_q1" in script and "--nodelist" not in script
    assert "#SBATCH --partition=compute" in script and "#SBATCH --gres=gpu:test:1" in script
    row["body"]["spec"] = dict(
        row["body"]["spec"], identity=dict(row["body"]["spec"]["identity"], task_type="training")
    )
    with pytest.raises(BackendError):
        backend.script(row)


def test_pool_result_binds_actual_node_or_excludes_the_profile(service, pair):
    from resource_advisor.contracts import ExecutionResult, Measurements, State

    profile = pool_fixture(service, pair)
    plan = preview(service, profile, candidate_ref="base")
    outcomes = {}
    for key, node in [("inside", "spare-node"), ("outside", "rogue-node")]:
        job = service.submit(
            "team-a",
            JobRequest(
                workload_ref="pair-work",
                candidate_ref="base",
                scheduling_profile_ref=profile.ref,
                scheduling_plan_digest=plan["digest"],
            ),
            "pool-" + key,
        )
        with service.store.transaction() as conn:
            row = service.store.job(conn, job["job_id"])
            body = dict(row["body"], placement={"source": "fixture", "node_name": node})
            service.store.change_job(conn, row, State.COLLECTING, body)
        b = row["body"]
        result = ExecutionResult(
            job_id=job["job_id"],
            attempt_id=b["attempt_id"],
            epoch=1,
            workload_signature=b["workload_signature"],
            context_signature=b["context_signature"],
            evidence_kind="synthetic",
            outcome="COMPLETED",
            measurements=Measurements(
                elapsed_seconds=1,
                peak_memory_mib=1,
                quality_value=1,
                sample_count=10,
                work_units=10,
            ),
        )
        service.ingest("team-a", result, signature(result))
        with service.store.transaction() as conn:
            outcomes[key] = dict(service.store.job(conn, job["job_id"])["body"])
            outcomes[key]["profiles"] = [
                p["body"]
                for p in service.store.list(conn, "profile", "team-a")
                if p["body"]["job_id"] == job["job_id"]
            ]
    assert outcomes["inside"]["actual_capability_ref"] == "spare-cap"
    assert outcomes["inside"]["profiles"][0]["actual_node_ref"] == "spare-node"
    assert outcomes["outside"]["profile_excluded"] == "ACTUAL_NODE_OUTSIDE_POOL"
    assert outcomes["outside"]["profiles"] == []


def test_api_imports_evidence_as_operator_and_exposes_decision_linkage(service, pair):
    spec, variant, caps = pair
    tokens = {
        "operator": Principal("team-a", True),
        "user": Principal("team-a"),
        "other": Principal("team-b"),
    }
    client = TestClient(
        create_app(service, {hashlib.sha256(k.encode()).hexdigest(): v for k, v in tokens.items()})
    )
    url = "/api/v1/compute"

    def h(token):
        return {"Authorization": "Bearer " + token}

    fast = jct_profile(spec, variant, caps["spare"], "spare", 1.0).model_dump(mode="json")
    assert client.post(url + "/jct-profiles", headers=h("user"), json=fast).status_code == 403
    assert client.post(url + "/jct-profiles", headers=h("operator"), json=fast).status_code == 200
    future = dict(fast, ref="future", measured_at=(now() + timedelta(hours=1)).isoformat())
    assert client.post(url + "/jct-profiles", headers=h("operator"), json=future).status_code == 422
    profile = scheduling_profile(recommendation={"policy": "profile_only", "fallback": "legacy"})
    assert (
        client.post(
            url + "/scheduling-profiles",
            headers=h("operator"),
            json=profile.model_dump(mode="json"),
        ).status_code
        == 200
    )
    response = client.post(
        url + "/jobs",
        headers={**h("user"), "Idempotency-Key": "api-hetero"},
        json={"workload_ref": spec.ref, "scheduling_profile_ref": profile.ref},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    link = client.get(url + f"/jobs/{job_id}/recommendation", headers=h("user")).json()
    assert link["candidate_ref"] == "spare"
    assert link["recommendation"]["status"] == "SELECTED"
    assert client.get(url + f"/jobs/{job_id}/recommendation", headers=h("other")).status_code == 404


def test_collectors_derive_profiles_only_from_complete_native_evidence(service, pair):
    rows = []
    base = now() - timedelta(minutes=10)
    result = {
        "evidence_kind": "hardware",
        "outcome": "COMPLETED",
        "attempt_id": "a-1",
        "measurements": {"elapsed_seconds": 4.0, "peak_memory_mib": 100.0},
    }
    spec, variant, caps = pair
    for n in range(2):
        body = {
            "spec": spec.model_dump(mode="json"),
            "candidate": spec.candidates[0].model_dump(mode="json"),
            "capability": caps["base"].model_dump(mode="json"),
            "workload_signature": signature(spec.identity),
            "context_signature": context_signature(spec.candidates[0], variant),
            "attempt_id": "a-1",
            "quality_passed": True,
            "result_digest": signature(result),
            "placement": {"node_name": "test-worker"},
            "scheduler_submitted_at": (base + timedelta(seconds=n)).isoformat(),
            "started_at": (base + timedelta(seconds=n + 2)).isoformat(),
            "execution_started_at": (base + timedelta(seconds=n + 5)).isoformat(),
            "backend_finished_at": (base + timedelta(seconds=n + 10)).isoformat(),
        }
        rows.append({"id": f"j-{n}", "state": "SUCCEEDED", "body": body, "result": result})
    hardware = HardwareFingerprint(
        accelerator_vendor="nvidia",
        accelerator_model="fixture-gpu",
        device_class="gpu",
        memory_model="unified",
        allocation_mode="physical_device",
        logical_slot=False,
    )
    made = jct_profile_from_jobs(rows, ref="derived", valid_for_seconds=600, hardware=hardware)
    assert made.admission_seconds == 2.0
    assert made.phases == {"runtime_startup": 3.0, "compute": 4.0, "unattributed_in_container": 1.0}
    assert made.staging_boundary == "inside_compute" and made.repeats == 2
    rows[1]["body"] = dict(rows[1]["body"], placement={"node_name": "other"})
    with pytest.raises(ValueError, match="node"):
        jct_profile_from_jobs(rows, ref="mixed", valid_for_seconds=600, hardware=hardware)
    pods = [
        {
            "metadata": {"name": "svc", "namespace": "edge", "annotations": {}},
            "spec": {
                "nodeName": "test-worker",
                "containers": [{"resources": {"requests": {"nvidia.com/gpu": "1"}}}],
            },
            "status": {"phase": "Running", "startTime": now().isoformat()},
        }
    ]
    snapshot = queue_snapshot_from_pods(
        pods,
        ref="q",
        project_ref="team-a",
        cluster_ref="lab",
        route_ref="test-worker",
        nodes={"test-worker"},
        resource_key="nvidia.com/gpu",
        capacity=1,
        capacity_unit="physical_device",
        observed_at=now(),
        evidence_ref="fixture",
    )
    assert not snapshot.complete
    assert snapshot.incomplete_reasons == ("OCCUPANCY_UNKNOWN:external:edge/svc",)
