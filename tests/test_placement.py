"""Role-placement fixtures are hand-written topology, not measured hardware evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from resource_advisor.placement import (
    PlacementRequest,
    ResolvedCandidate,
    TopologySnapshot,
    plan_placement,
)

AT = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def endpoint(ref, node, *, backend="kubernetes", host=None, devices=()):
    return {
        "endpoint_ref": ref,
        "backend": backend,
        "cluster_ref": "lab",
        "node_ref": node,
        "physical_host_ref": host,
        "devices": [{"device_ref": d, "resource_key": key} for d, key in devices],
    }


def link(source, target, bandwidth, overhead, measured=None):
    return {
        "source_endpoint_ref": source,
        "target_endpoint_ref": target,
        "bandwidth_bytes_per_second": bandwidth,
        "request_overhead_ms": overhead,
        "measured_at": (measured or AT - timedelta(minutes=5)).isoformat(),
        "evidence_ref": "iperf-fixture",
    }


def snapshot(links, endpoints=None, **changes):
    body = {
        "ref": "topo-1",
        "project_ref": "team-a",
        "source": "operator_import",
        "evidence_ref": "topology-fixture",
        "measurement_method": "fixture directed transfer probe",
        "observed_at": (AT - timedelta(minutes=1)).isoformat(),
        "expires_at": (AT + timedelta(hours=1)).isoformat(),
        "endpoints": endpoints
        or [
            endpoint("ep-npu", "npu-node", devices=[("npu-0", "mobilint.com/npu")]),
            endpoint("ep-gpu-a", "gpu-a", devices=[("gpu-a0", "nvidia.com/gpu")]),
            endpoint("ep-gpu-b", "gpu-b", devices=[("gpu-b0", "nvidia.com/gpu")]),
        ],
        "links": links,
        **changes,
    }
    return TopologySnapshot.model_validate(body)


def request(edges=None, roles=None, **changes):
    return PlacementRequest.model_validate(
        {
            "task_ref": "teacher-student",
            "scheduling_profile_ref": "interactive-v1",
            "topology_snapshot_ref": "topo-1",
            "roles": roles
            or [
                {"role_ref": "teacher", "workload_ref": "teacher-forward"},
                {"role_ref": "student", "workload_ref": "student-train"},
            ],
            "edges": edges
            if edges is not None
            else [
                {
                    "producer_role": "teacher",
                    "consumer_role": "student",
                    "bytes_per_transfer": 1_000_000,
                    "transfer_count": 1,
                }
            ],
            **changes,
        }
    )


def candidate(role, ref, node, *, device_class="gpu", key="nvidia.com/gpu", **changes):
    body = {
        "role_ref": role,
        "workload_ref": "teacher-forward" if role == "teacher" else "student-train",
        "candidate_ref": ref,
        "backend": "kubernetes",
        "cluster_ref": "lab",
        "node_ref": node,
        "device_class": device_class,
        "accelerator_model": "fixture-" + device_class,
        "resource_key": key,
        "allocation_mode": "physical_device",
        "resources": {"host_cpu": 1, "host_memory_mib": 512, "accelerator_count": 1},
        "availability": "available",
        "headroom": {"cpu": 4, "memory": 8 * 2**30, key: 1},
        "reasons": [],
        **changes,
    }
    return ResolvedCandidate.model_validate(body)


def teacher_student():
    return [
        candidate("teacher", "npu", "npu-node", device_class="npu", key="mobilint.com/npu"),
        candidate("student", "gpu-a", "gpu-a"),
        candidate("student", "gpu-b", "gpu-b"),
    ]


def test_measured_directed_link_changes_selected_student_and_reports_hand_worked_cost():
    slow_a = [link("ep-npu", "ep-gpu-a", 1_000_000, 2), link("ep-npu", "ep-gpu-b", 4_000_000, 1)]
    plan = plan_placement(snapshot(slow_a), request(), teacher_student(), at=AT)
    assert plan["accepted"]
    assert plan["selected"]["assignment"]["student"]["candidate_ref"] == "gpu-b"
    # 1,000,000 B at 4,000,000 B/s = 250 ms, plus 1 ms request overhead.
    assert plan["selected"]["estimated_communication_ms"] == 251.0
    ranked = {tuple(c["assignment"].values()): c for c in plan["combinations"]}
    # 1,000,000 B at 1,000,000 B/s = 1000 ms, plus 2 ms request overhead.
    assert ranked[("npu", "gpu-a")]["estimated_communication_ms"] == 1002.0
    assert ranked[("npu", "gpu-a")]["transfers"][0]["link"]["status"] == "measured"

    slow_b = [link("ep-npu", "ep-gpu-a", 1_000_000, 2), link("ep-npu", "ep-gpu-b", 100_000, 1)]
    plan = plan_placement(snapshot(slow_b), request(), teacher_student(), at=AT)
    assert plan["selected"]["assignment"]["student"]["candidate_ref"] == "gpu-a"
    assert plan["selected"]["estimated_communication_ms"] == 1002.0


def test_reverse_link_is_not_inferred_and_unknown_sorts_after_measured_soft_alternative():
    links = [link("ep-npu", "ep-gpu-a", 1_000_000, 2), link("ep-gpu-b", "ep-npu", 1e12, 0)]
    plan = plan_placement(snapshot(links), request(), teacher_student(), at=AT)
    assert plan["selected"]["assignment"]["student"]["candidate_ref"] == "gpu-a"
    second = plan["combinations"][1]
    assert second["assignment"] == {"teacher": "npu", "student": "gpu-b"}
    assert second["status"] == "partially_unknown"
    assert second["unknowns"] == ["teacher->student:LINK_MISSING"]
    assert second["estimated_communication_ms"] is None


def test_hard_network_requirement_fails_closed_for_missing_and_expired_links():
    hard = [
        {
            "producer_role": "teacher",
            "consumer_role": "student",
            "bytes_per_transfer": 1_000_000,
            "transfer_count": 1,
            "requirement": "hard",
            "min_bandwidth_bytes_per_second": 500_000,
        }
    ]
    links = [link("ep-npu", "ep-gpu-a", 1_000_000, 2)]
    plan = plan_placement(snapshot(links), request(hard), teacher_student(), at=AT)
    assert [c["assignment"]["student"] for c in plan["combinations"]] == ["gpu-a"]
    assert plan["excluded_combinations"]["items"] == [
        {
            "assignment": {"teacher": "npu", "student": "gpu-b"},
            "reasons": ["teacher->student:HARD_LINK_MISSING"],
        }
    ]

    later = AT + timedelta(hours=2)
    expired = plan_placement(snapshot(links), request(hard), teacher_student(), at=later)
    assert expired["snapshot"]["status"] == "expired"
    assert not expired["accepted"]
    assert expired["reasons"] == ["NO_FEASIBLE_COMBINATION"]
    assert expired["excluded_combinations"]["reason_counts"] == {
        "HARD_LINK_EXPIRED": 1,
        "HARD_LINK_MISSING": 1,
        "PHYSICAL_DEVICE_IDENTITY_EXPIRED": 4,
    }

    slow = [link("ep-npu", "ep-gpu-a", 100_000, 2), link("ep-npu", "ep-gpu-b", 100_000, 2)]
    blocked = plan_placement(snapshot(slow), request(hard), teacher_student(), at=AT)
    assert not blocked["accepted"]
    assert blocked["excluded_combinations"]["reason_counts"] == {"BANDWIDTH_BELOW_MINIMUM": 2}


def test_backend_aliases_share_physical_devices_and_host_capacity():
    alias = [
        endpoint(
            "ep-k8s",
            "gpu-host",
            host="host-1",
            devices=[("gpu-0", "nvidia.com/gpu"), ("gpu-1", "nvidia.com/gpu")],
        ),
        endpoint(
            "ep-slurm",
            "gpu-host-slurm",
            backend="slurm",
            host="host-1",
            devices=[("gpu-0", "nvidia.com/gpu"), ("gpu-1", "nvidia.com/gpu")],
        ),
    ]
    links = [link("ep-k8s", "ep-slurm", 1e9, 0)]
    roles = [
        {"role_ref": "teacher", "workload_ref": "teacher-forward"},
        {"role_ref": "student", "workload_ref": "student-train"},
    ]
    k8s = candidate(
        "teacher", "k8s", "gpu-host", headroom={"cpu": 4, "memory": 2**33, "nvidia.com/gpu": 2}
    )
    slurm = candidate(
        "student",
        "slurm",
        "gpu-host-slurm",
        backend="slurm",
        headroom={"cpu": 4, "memory": 2**33, "nvidia.com/gpu": 2},
    )
    plan = plan_placement(snapshot(links, alias), request(roles=roles), [k8s, slurm], at=AT)
    assert plan["selected"]["pinned_device_refs"] == {"teacher": ["gpu-0"], "student": ["gpu-1"]}
    assert plan["selected"]["admission"] == "observed_headroom_recheck_at_submission"
    assert plan["selected"]["transfers"][0]["same_physical_host"] is True

    two = {"host_cpu": 1, "host_memory_mib": 512, "accelerator_count": 2}
    greedy = candidate(
        "teacher",
        "k8s",
        "gpu-host",
        resources=two,
        headroom={"cpu": 4, "memory": 2**33, "nvidia.com/gpu": 2},
    )
    plan = plan_placement(snapshot(links, alias), request(roles=roles), [greedy, slurm], at=AT)
    assert not plan["accepted"]
    # Each alias reports two free GPUs, but the host has two physical GPUs in total.
    assert plan["excluded_combinations"]["items"][0]["reasons"] == [
        "host-1:SHARED_CAPACITY_EXCEEDED:nvidia.com/gpu",
        "student:DISTINCT_PHYSICAL_DEVICE_INSUFFICIENT",
    ]

    divergent = candidate(
        "student",
        "slurm",
        "gpu-host-slurm",
        backend="slurm",
        headroom={"cpu": 1.5, "memory": None, "nvidia.com/gpu": 2},
    )
    plan = plan_placement(snapshot(links, alias), request(roles=roles), [k8s, divergent], at=AT)
    assert plan["excluded_combinations"]["items"][0]["reasons"] == [
        "host-1:SHARED_CAPACITY_EXCEEDED:cpu",
        "host-1:SHARED_CAPACITY_UNKNOWN:memory",
    ]


def test_unknown_physical_identity_and_virtual_slots_cannot_claim_distinct_devices():
    links = [link("ep-npu", "ep-gpu-a", 1e9, 0)]
    unnamed = [
        endpoint("ep-npu", "npu-node"),
        endpoint("ep-gpu-a", "gpu-a", devices=[("gpu-a0", "nvidia.com/gpu")]),
    ]
    teacher = candidate("teacher", "npu", "npu-node", device_class="npu", key="mobilint.com/npu")
    student = candidate("student", "gpu-a", "gpu-a")
    plan = plan_placement(snapshot(links, unnamed), request(), [teacher, student], at=AT)
    assert plan["excluded_combinations"]["reason_counts"] == {"PHYSICAL_DEVICE_IDENTITY_UNKNOWN": 1}

    slot = candidate("student", "gpu-a", "gpu-a", allocation_mode="virtual_slot")
    plan = plan_placement(snapshot(links), request(), [teacher, slot], at=AT)
    assert plan["excluded_combinations"]["reason_counts"] == {"VIRTUAL_SLOT_NOT_PHYSICAL_DEVICE": 1}


def test_old_link_inside_fresh_snapshot_is_expired_not_laundered():
    old = link("ep-npu", "ep-gpu-a", 1_000_000, 2)
    old["measured_at"] = (AT - timedelta(minutes=50)).isoformat()
    fresh = link("ep-npu", "ep-gpu-b", 100_000, 2)
    plan = plan_placement(
        snapshot([old, fresh]), request(), teacher_student(), at=AT + timedelta(minutes=15)
    )
    assert plan["snapshot"]["status"] == "fresh"
    assert plan["selected"]["assignment"]["student"]["candidate_ref"] == "gpu-b"
    expired = plan["combinations"][1]
    assert expired["unknowns"] == ["teacher->student:LINK_EXPIRED"]

    ancient = {**old, "measured_at": (AT - timedelta(hours=3)).isoformat()}
    with pytest.raises(ValidationError, match="link_max_age"):
        snapshot([ancient])


@pytest.mark.parametrize(
    "change,message",
    [
        ({"observed_at": (AT + timedelta(hours=2)).isoformat()}, "expires_at"),
        ({"expires_at": (AT + timedelta(days=8)).isoformat()}, "expires_at"),
        ({"links": [link("ep-npu", "ep-gpu-a", 1e6, 1)] * 2}, "duplicate ambiguous"),
        ({"links": [link("ep-npu", "ep-gpu-a", float("nan"), 1)]}, "finite"),
        ({"links": [link("ep-npu", "ep-gpu-a", float("inf"), 1)]}, "finite"),
        ({"links": [link("ep-npu", "ep-gpu-a", 0, 1)]}, "greater than 0"),
        ({"links": [link("ep-npu", "ep-missing", 1e6, 1)]}, "unknown endpoint"),
    ],
)
def test_snapshot_rejects_inconsistent_or_ambiguous_measurements(change, message):
    with pytest.raises(ValidationError, match=message):
        snapshot(change.pop("links", []), **change)


def edge(a, b):
    return {"producer_role": a, "consumer_role": b, "bytes_per_transfer": 1, "transfer_count": 1}


@pytest.mark.parametrize(
    "roles,edges,message",
    [
        ([{"role_ref": f"r{i}", "workload_ref": "w"} for i in range(5)], [], "at most 4"),
        ([{"role_ref": "a", "workload_ref": "w"}] * 2, [], "duplicate role"),
        (
            [{"role_ref": x, "workload_ref": "w"} for x in "abc"],
            [edge("a", "b"), edge("b", "c"), edge("c", "a")],
            "cycle",
        ),
        ([{"role_ref": "a", "workload_ref": "w"}], [edge("a", "z")], "unknown role"),
    ],
)
def test_role_graph_bounds_and_cycles_rejected(roles, edges, message):
    with pytest.raises(ValidationError, match=message):
        request(edges, roles)


def test_candidate_bound_and_deterministic_replay_digest():
    links = [link("ep-npu", "ep-gpu-a", 1_000_000, 2), link("ep-npu", "ep-gpu-b", 4_000_000, 1)]
    first = plan_placement(snapshot(links), request(), teacher_student(), at=AT)
    replay = plan_placement(snapshot(links), request(), list(reversed(teacher_student())), at=AT)
    assert replay["digest"] == first["digest"]
    assert first["digest"].startswith("sha256:")
    changed = plan_placement(snapshot(links[:1]), request(), teacher_student(), at=AT)
    assert changed["snapshot"]["digest"] != first["snapshot"]["digest"]
    assert changed["digest"] != first["digest"]

    many = [candidate("student", f"gpu-{i}", "gpu-a") for i in range(9)]
    with pytest.raises(ValueError, match="exceeds 8 candidates"):
        plan_placement(snapshot(links), request(), many, at=AT)


def test_ineligible_candidates_stay_visible_with_reasons():
    links = [link("ep-npu", "ep-gpu-a", 1_000_000, 2)]
    npu_student = candidate(
        "student",
        "npu-backward",
        "npu-node",
        device_class="npu",
        key="mobilint.com/npu",
        reasons=["CANDIDATE_CONTRACT_UNAVAILABLE"],
    )
    plan = plan_placement(snapshot(links), request(), teacher_student()[:2] + [npu_student], at=AT)
    shown = {c["candidate_ref"]: c for c in plan["candidates"]}
    assert shown["npu-backward"]["eligible"] is False
    assert shown["npu-backward"]["reasons"] == ["CANDIDATE_CONTRACT_UNAVAILABLE"]
    assert plan["combinations_evaluated"] == 1


def registered_teacher_student(service, bundle):
    """Inference NPU teacher plus training GPU student, registered like an operator would."""
    from resource_advisor.contracts import Candidate, ProfilingPolicy, WorkloadSpec, signature
    from resource_advisor.training import TrainingIsolation

    base_spec, base_candidate, base_variant, base_cap = bundle
    digest = base_spec.identity.model_digest
    runtime = {"cuda": "fixture", "firmware_identity": "fixture-fw"}
    caps, variants = {}, {}

    def device(ref, workload, node, vendor, device_class, key, verification):
        model = f"fixture-{vendor}"
        caps[ref] = base_cap.model_copy(
            update={
                "ref": "cap-" + ref,
                "node_ref": node,
                "accelerator_vendor": vendor,
                "accelerator_model": model,
                "device_class": device_class,
                "resource_key": key,
                "runtime_versions": runtime,
            }
        )
        variants[ref] = base_variant.model_copy(
            update={
                "ref": "variant-" + ref,
                "workload_ref": workload,
                "accelerator_vendor": vendor,
                "device_class": device_class,
                "verification": verification,
                "runtime_versions": runtime,
            }
        )
        context = base_candidate.context.model_copy(
            update={"accelerator_model": model, "runtime_versions": runtime}
        )
        return Candidate(
            ref=ref,
            variant_ref="variant-" + ref,
            capability_ref="cap-" + ref,
            backend="kubernetes",
            context=context,
        )

    teacher = base_spec.model_copy(
        update={
            "ref": "teacher-forward",
            "candidates": (
                device(
                    "npu",
                    "teacher-forward",
                    "npu-node",
                    "mobilint",
                    "npu",
                    "mobilint.com/npu",
                    "MODEL_VERIFIED",
                ),
            ),
            "baseline_candidate_ref": "npu",
        }
    )
    training = base_spec.identity.model_copy(
        update={"task_type": "training", "dataset_version": digest}
    )
    student = WorkloadSpec(
        ref="student-train",
        project_ref="team-a",
        identity=training,
        candidates=tuple(
            device(ref, "student-train", node, vendor, cls, key, verification)
            for ref, node, vendor, cls, key, verification in [
                ("gpu-a", "gpu-a", "nvidia", "gpu", "nvidia.com/gpu", "TRAINING_VERIFIED"),
                ("gpu-b", "gpu-b", "nvidia", "gpu", "nvidia.com/gpu", "TRAINING_VERIFIED"),
                (
                    "npu-backward",
                    "npu-node",
                    "mobilint",
                    "npu",
                    "mobilint.com/npu",
                    "MODEL_VERIFIED",
                ),
            ]
        ),
        baseline_candidate_ref="gpu-a",
        profiling=ProfilingPolicy(checkpoint_digest=digest),
        quality=base_spec.quality,
    )
    for ref in variants:
        spec = teacher if ref == "npu" else student
        variants[ref] = variants[ref].model_copy(
            update={"workload_signature": signature(spec.identity)}
        )
        service.register("variant", variants[ref], "team-a")
        service.register("capability", caps[ref], "team-a")
    service.register("workload", teacher, "team-a")
    service.register("workload", student, "team-a")
    service.register(
        "training_isolation",
        TrainingIsolation(
            ref="student-train",
            project_ref="team-a",
            workload_ref="student-train",
            workload_digest=signature(student),
            checkpoint_digest=digest,
            input_digest=digest,
            checkpoint_size_bytes=1024,
            input_size_bytes=1024,
            validation_refs=("fixture-training-validation",),
        ),
        "team-a",
    )


def test_api_plans_registered_roles_with_operator_snapshot_and_project_isolation(
    bundle, database_store
):
    import hashlib

    from fastapi.testclient import TestClient
    from test_scheduling import profile

    from resource_advisor.api import Principal, create_app
    from resource_advisor.contracts import now
    from resource_advisor.service import Service

    service = Service(database_store, accept_synthetic=True, operational_mode=True)
    registered_teacher_student(service, bundle)
    service.register("scheduling_profile", profile(), "team-a")
    tokens = {
        "operator": Principal("team-a", True),
        "user": Principal("team-a"),
        "other": Principal("team-b"),
    }
    client = TestClient(
        create_app(service, {hashlib.sha256(k.encode()).hexdigest(): v for k, v in tokens.items()})
    )
    url = "/api/v1/compute"

    def call(method, path, token, **kwargs):
        return client.request(
            method, url + path, headers={"Authorization": "Bearer " + token}, **kwargs
        )

    legacy = {"profile_ref": "interactive-v1", "workload_ref": "student-train"}
    before = call("POST", "/scheduling-plans", "user", json=legacy)
    assert before.status_code == 200

    observed = now() - timedelta(seconds=30)
    body = snapshot(
        [
            link("ep-npu", "ep-gpu-a", 1_000_000, 2, observed),
            link("ep-npu", "ep-gpu-b", 4_000_000, 1, observed),
        ],
        observed_at=observed.isoformat(),
        expires_at=(observed + timedelta(hours=1)).isoformat(),
    ).model_dump(mode="json")
    assert call("POST", "/topology-snapshots", "user", json=body).status_code == 403
    assert call("POST", "/topology-snapshots", "operator", json=body).status_code == 200
    changed = {**body, "evidence_ref": "other-evidence"}
    assert call("POST", "/topology-snapshots", "operator", json=changed).status_code == 409
    future = {
        **body,
        "ref": "topo-future",
        "observed_at": (now() + timedelta(minutes=5)).isoformat(),
        "expires_at": (now() + timedelta(minutes=30)).isoformat(),
    }
    assert call("POST", "/topology-snapshots", "operator", json=future).status_code == 422
    assert call("GET", "/topology-snapshots/topo-1", "user").json()["ref"] == "topo-1"
    assert call("GET", "/topology-snapshots/topo-1", "other").status_code == 404
    assert call("GET", "/topology-snapshots", "other").json() == {"items": []}

    payload = request().model_dump(mode="json")
    response = call("POST", "/placement-plans", "user", json=payload)
    assert response.status_code == 200
    plan = response.json()
    assert plan["accepted"]
    selected = plan["selected"]["assignment"]
    assert (selected["teacher"]["candidate_ref"], selected["student"]["candidate_ref"]) == (
        "npu",
        "gpu-b",
    )
    assert selected["teacher"]["accelerator_model"] == "fixture-mobilint"
    assert plan["selected"]["estimated_communication_ms"] == 251.0
    # No inventory was imported: headroom is unknown and must not read as runnable now.
    assert plan["selected"]["admission"] == "unknown"
    shown = {c["candidate_ref"]: c for c in plan["candidates"]}
    assert shown["npu-backward"]["eligible"] is False
    assert "CANDIDATE_CONTRACT_UNAVAILABLE" in shown["npu-backward"]["reasons"]
    assert shown["npu-backward"]["device_class"] == "npu"
    assert "example.invalid" not in response.text and "workload.py" not in response.text
    assert call("POST", "/placement-plans", "other", json=payload).status_code == 404

    after = call("POST", "/scheduling-plans", "user", json=legacy)
    assert after.json() == before.json()


def test_same_node_gpu_and_npu_colocate_with_ordinary_headroom_but_oversubscription_fails():
    mixed = [
        endpoint(
            "ep-mixed",
            "mixed",
            devices=[
                ("npu-0", "mobilint.com/npu"),
                ("gpu-0", "nvidia.com/gpu"),
                ("gpu-1", "nvidia.com/gpu"),
            ],
        )
    ]
    links = [link("ep-mixed", "ep-mixed", 1e9, 0)]
    npu = candidate("teacher", "npu", "mixed", device_class="npu", key="mobilint.com/npu")
    gpu = candidate("student", "gpu", "mixed")
    plan = plan_placement(snapshot(links, mixed), request(), [npu, gpu], at=AT)
    assert plan["accepted"], plan["excluded_combinations"]
    assert plan["selected"]["status"] == "complete"
    assert plan["selected"]["pinned_device_refs"] == {"teacher": ["npu-0"], "student": ["gpu-0"]}

    # Two GPU roles on one node each see one free GPU; together they need two.
    gpu_teacher = candidate("teacher", "gpu-t", "mixed")
    plan = plan_placement(snapshot(links, mixed), request(), [gpu_teacher, gpu], at=AT)
    assert not plan["accepted"]
    assert plan["excluded_combinations"]["items"][0]["reasons"] == [
        "kubernetes/lab/mixed:SHARED_CAPACITY_EXCEEDED:nvidia.com/gpu"
    ]

    # A non-owner that explicitly reports the key unknown still fails closed.
    hedged = candidate(
        "teacher",
        "npu",
        "mixed",
        device_class="npu",
        key="mobilint.com/npu",
        headroom={"cpu": 4, "memory": 8 * 2**30, "mobilint.com/npu": 1, "nvidia.com/gpu": None},
    )
    plan = plan_placement(snapshot(links, mixed), request(), [hedged, gpu], at=AT)
    assert plan["excluded_combinations"]["items"][0]["reasons"] == [
        "kubernetes/lab/mixed:SHARED_CAPACITY_UNKNOWN:nvidia.com/gpu"
    ]
    owner_missing = candidate("student", "gpu", "mixed", headroom={"cpu": 4, "memory": 2**33})
    plan = plan_placement(snapshot(links, mixed), request(), [npu, owner_missing], at=AT)
    assert plan["excluded_combinations"]["items"][0]["reasons"] == [
        "kubernetes/lab/mixed:SHARED_CAPACITY_UNKNOWN:nvidia.com/gpu"
    ]


def test_expired_snapshot_costs_never_rank_and_identity_cannot_be_guaranteed():
    # Stale probes say gpu-b is four times faster; after expiry that must not matter.
    slow_a = [link("ep-npu", "ep-gpu-a", 1_000_000, 2), link("ep-npu", "ep-gpu-b", 4_000_000, 1)]
    later = AT + timedelta(hours=2)
    loose = request(require_distinct_physical_devices=False)
    plan = plan_placement(snapshot(slow_a), loose, teacher_student(), at=later)
    assert plan["snapshot"]["status"] == "expired"
    selected = plan["selected"]
    assert selected["assignment"]["student"]["candidate_ref"] == "gpu-a"
    assert selected["status"] == "partially_unknown"
    assert selected["estimated_communication_ms"] is None
    assert "teacher->student:LINK_EXPIRED" in selected["unknowns"]
    assert "TOPOLOGY_SNAPSHOT_EXPIRED" in selected["unknowns"]
    transfer = selected["transfers"][0]
    assert transfer["estimated_total_ms"] is None
    assert transfer["link"]["status"] == "expired"
    assert transfer["link"]["bandwidth_bytes_per_second"] == 1_000_000  # provenance only
    for combo in plan["combinations"]:
        assert combo["estimated_communication_ms"] is None
        assert combo["known_partial_communication_ms"] == 0

    # No edges: an expired identity listing cannot give a complete distinct-device plan.
    plan = plan_placement(snapshot(slow_a), request(edges=[]), teacher_student(), at=later)
    assert not plan["accepted"]
    assert plan["excluded_combinations"]["reason_counts"] == {"PHYSICAL_DEVICE_IDENTITY_EXPIRED": 4}
    plan = plan_placement(
        snapshot(slow_a),
        request(edges=[], require_distinct_physical_devices=False),
        teacher_student(),
        at=later,
    )
    assert plan["selected"]["status"] == "partially_unknown"
    assert plan["selected"]["unknowns"] == ["TOPOLOGY_SNAPSHOT_EXPIRED"]


def test_pure_planner_rejects_candidates_outside_role_workload_or_restriction():
    links = [link("ep-npu", "ep-gpu-a", 1e9, 0)]
    wrong = candidate("student", "gpu-a", "gpu-a", workload_ref="teacher-forward")
    with pytest.raises(ValueError, match="workload_ref"):
        plan_placement(snapshot(links), request(), teacher_student()[:1] + [wrong], at=AT)
    roles = [
        {"role_ref": "teacher", "workload_ref": "teacher-forward"},
        {"role_ref": "student", "workload_ref": "student-train", "candidate_refs": ["gpu-a"]},
    ]
    with pytest.raises(ValueError, match="candidate_refs"):
        plan_placement(snapshot(links), request(roles=roles), teacher_student(), at=AT)
    plan = plan_placement(snapshot(links), request(roles=roles), teacher_student()[:2], at=AT)
    assert plan["accepted"]


def test_distinct_device_pinning_finds_constrained_matching_and_rejects_double_use():
    shared = [
        endpoint(
            "ep-gpu",
            "gpu-host",
            devices=[("gpu-a", "nvidia.com/gpu"), ("gpu-b", "nvidia.com/gpu")],
        ),
        endpoint(
            "ep-gpu-slurm",
            "gpu-host-slurm",
            backend="slurm",
            host="host-1",
            devices=[("gpu-a", "nvidia.com/gpu")],
        ),
    ]
    # ep-gpu shares host-1 so gpu-a is one physical device seen through two backends.
    shared[0]["physical_host_ref"] = "host-1"
    links = [link("ep-gpu", "ep-gpu-slurm", 1e9, 0)]
    room = {"cpu": 4, "memory": 2**33, "nvidia.com/gpu": 2}
    first = candidate("teacher", "k8s", "gpu-host", headroom=room)
    only_a = candidate("student", "slurm", "gpu-host-slurm", backend="slurm", headroom=room)
    plan = plan_placement(snapshot(links, shared), request(), [first, only_a], at=AT)
    assert plan["accepted"], plan["excluded_combinations"]
    assert plan["selected"]["pinned_device_refs"] == {"teacher": ["gpu-b"], "student": ["gpu-a"]}

    narrow = [
        shared[0] | {"devices": [{"device_ref": "gpu-a", "resource_key": "nvidia.com/gpu"}]},
        shared[1],
    ]
    plan = plan_placement(snapshot(links, narrow), request(), [first, only_a], at=AT)
    assert not plan["accepted"]
    assert plan["excluded_combinations"]["reason_counts"] == {
        "DISTINCT_PHYSICAL_DEVICE_INSUFFICIENT": 1
    }


def test_api_uses_only_inventory_from_the_candidates_own_cluster(bundle, database_store):
    import hashlib

    from fastapi.testclient import TestClient
    from test_scheduling import profile

    from resource_advisor.api import Principal, create_app
    from resource_advisor.contracts import now
    from resource_advisor.inventory import save_inventory, signal
    from resource_advisor.service import Service

    service = Service(database_store, accept_synthetic=True, operational_mode=True)
    registered_teacher_student(service, bundle)
    service.register("scheduling_profile", profile(), "team-a")
    client = TestClient(
        create_app(service, {hashlib.sha256(b"op").hexdigest(): Principal("team-a", True)})
    )
    headers = {"Authorization": "Bearer op"}
    observed = now() - timedelta(seconds=30)
    body = snapshot(
        [link("ep-npu", "ep-gpu-b", 4_000_000, 1, observed)],
        observed_at=observed.isoformat(),
        expires_at=(observed + timedelta(hours=1)).isoformat(),
    ).model_dump(mode="json")
    url = "/api/v1/compute"
    assert client.post(url + "/topology-snapshots", json=body, headers=headers).status_code == 200

    def inventory(ref, cluster, collected, nodes):
        stamp = collected.isoformat()
        save_inventory(
            service.store,
            "team-a",
            {
                "ref": ref,
                "cluster_ref": cluster,
                "collected_at": stamp,
                "stale_after_seconds": 120,
                "status": "ok",
                "nodes": [
                    {
                        "node_ref": node,
                        "ready": signal(ready, observed_at=stamp, source="fixture"),
                        "resources": {
                            k: {"request_headroom": signal(v, observed_at=stamp, source="fixture")}
                            for k, v in {"cpu": 8, "memory": 2**34, "nvidia.com/gpu": 1}.items()
                        },
                    }
                    for node, ready in nodes
                ],
            },
        )

    def shown():
        response = client.post(
            url + "/placement-plans", json=request().model_dump(mode="json"), headers=headers
        )
        assert response.status_code == 200
        return {c["candidate_ref"]: c for c in response.json()["candidates"]}

    inventory("inv-lab", "lab", now() - timedelta(seconds=20), [("gpu-b", True)])
    assert shown()["gpu-b"]["availability"] == "available"
    assert shown()["gpu-b"]["headroom"]["nvidia.com/gpu"] == 1

    # Same hostnames in another cluster are newer; the legacy observation key has no cluster.
    inventory(
        "inv-other", "elsewhere", now() - timedelta(seconds=5), [("gpu-b", True), ("gpu-a", False)]
    )
    view = shown()
    assert view["gpu-b"]["availability"] == "unknown"
    assert view["gpu-b"]["headroom"] == {}
    assert view["gpu-a"]["eligible"] is True
    assert view["gpu-a"]["availability"] == "unknown"
