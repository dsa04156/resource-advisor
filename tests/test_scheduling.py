"""Policy compilation fixtures do not represent measured hardware performance."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import BackendError, KubernetesBackend
from resource_advisor.contracts import JobRequest
from resource_advisor.scheduling import SchedulingPlanRequest, SchedulingProfile, compile_plan
from resource_advisor.service import Rejected


def profile(**policy):
    return SchedulingProfile.model_validate(
        {
            "ref": "interactive-v1",
            "project_ref": "team-a",
            "name": "Interactive",
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
                    "runtime_class_name": "gpu-runtime",
                    "priority_map": {"normal": None, "high": "urgent"},
                }
            ],
        }
    )


def preview(service, value=None, **request):
    value = value or profile()
    service.register("scheduling_profile", value, "team-a")
    with service.store.transaction() as conn:
        return compile_plan(
            service,
            conn,
            "team-a",
            SchedulingPlanRequest(profile_ref=value.ref, workload_ref="workload-1", **request),
        )


def test_profile_plan_is_enforced_in_job_and_native_manifest(service):
    result = preview(service, profile(priority="high"))
    explicit = preview(service, profile(priority="high"), candidate_ref="base")
    assert result["digest"] == explicit["digest"]
    request = JobRequest(
        workload_ref="workload-1",
        candidate_ref="base",
        scheduling_profile_ref="interactive-v1",
        scheduling_plan_digest=result["digest"],
    )
    job = service.submit("team-a", request, "policy-submit")
    assert service.submit("team-a", request, "policy-submit")["job_id"] == job["job_id"]
    with service.store.transaction() as conn:
        row = dict(service.store.job(conn, job["job_id"]))
    assert row["body"]["spec"]["execution"]["max_run_seconds"] == 30
    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        runtime_class_name="gpu-runtime",
        priority_classes={"high": "urgent"},
    )
    manifest = backend.manifest(row)
    assert manifest["spec"]["activeDeadlineSeconds"] == 30
    assert manifest["spec"]["template"]["spec"]["runtimeClassName"] == "gpu-runtime"
    assert manifest["metadata"]["labels"]["kueue.x-k8s.io/priority-class"] == "urgent"
    backend.local_queue = "different-queue"
    with pytest.raises(BackendError, match="SCHEDULING_BINDING_DRIFT"):
        backend.manifest(row)


@pytest.mark.parametrize(
    "policy,reason",
    [
        ({"quota_scope": "global"}, "GLOBAL_QUOTA_UNSUPPORTED"),
        ({"preemption": "never"}, "PREEMPTION_OVERRIDE_UNSUPPORTED"),
        ({"allocation_modes": ["virtual_slot"]}, "ALLOCATION_MODE_DENIED"),
        (
            {"max_resources": {"host_cpu": 1, "host_memory_mib": 256, "accelerator_count": 1}},
            "PROFILE_RESOURCE_LIMIT",
        ),
    ],
)
def test_unsupported_policy_rejected_before_submission(service, policy, reason):
    result = preview(service, profile(**policy))
    assert not result["accepted"]
    assert reason in result["excluded"]["base"]


def test_changed_plan_digest_rejected(service):
    preview(service)
    with pytest.raises(Rejected, match="SCHEDULING_PLAN_CHANGED"):
        service.submit(
            "team-a",
            JobRequest(
                workload_ref="workload-1",
                candidate_ref="base",
                scheduling_profile_ref="interactive-v1",
                scheduling_plan_digest="sha256:" + "0" * 64,
            ),
            "bad-plan",
        )


def test_profile_api_operator_scope_and_immutability(service):
    credentials = {
        hashlib.sha256(k.encode()).hexdigest(): v
        for k, v in {
            "operator": Principal("team-a", True),
            "user": Principal("team-a"),
            "other": Principal("team-b"),
        }.items()
    }
    client = TestClient(create_app(service, credentials))
    url = "/api/v1/compute"
    payload = profile().model_dump(mode="json")

    def headers(token):
        return {"Authorization": "Bearer " + token}

    assert (
        client.post(url + "/scheduling-profiles", headers=headers("user"), json=payload).status_code
        == 403
    )
    assert (
        client.post(
            url + "/scheduling-profiles", headers=headers("operator"), json=payload
        ).status_code
        == 200
    )
    payload["name"] = "changed"
    assert (
        client.post(
            url + "/scheduling-profiles", headers=headers("operator"), json=payload
        ).status_code
        == 409
    )
    body = {"profile_ref": "interactive-v1", "workload_ref": "workload-1"}
    assert client.post(url + "/scheduling-plans", headers=headers("user"), json=body).json()[
        "accepted"
    ]
    assert (
        client.post(url + "/scheduling-plans", headers=headers("other"), json=body).status_code
        == 404
    )


def test_auto_selection_and_slurm_qos_are_compiled_above_backends(service, bundle):
    from test_backends import native_runtime

    from resource_advisor.backends import SlurmBackend

    spec, candidate, variant, cap = bundle
    slurm_cap = cap.model_copy(
        update={
            "ref": "slurm-cap",
            "backend": "slurm",
            "backend_cluster_id": "hpc",
            "resource_key": "gpu:test",
        }
    )
    slurm_candidate = candidate.model_copy(
        update={"ref": "hpc", "backend": "slurm", "capability_ref": slurm_cap.ref}
    )
    spec = spec.model_copy(
        update={"ref": "dual-backend", "candidates": (candidate, slurm_candidate)}
    )
    variant = variant.model_copy(update={"ref": "dual-variant", "workload_ref": spec.ref})
    spec = spec.model_copy(
        update={
            "candidates": tuple(
                c.model_copy(update={"variant_ref": variant.ref}) for c in spec.candidates
            )
        }
    )
    for kind, model in [("capability", slurm_cap), ("variant", variant), ("workload", spec)]:
        service.register(kind, model, "team-a")
    service.operational_mode = True
    body = profile(backend_order=["slurm", "kubernetes"]).model_dump(mode="json")
    body["bindings"].append(
        {
            "backend": "slurm",
            "cluster_ref": "hpc",
            "partition": "compute",
            "account": "team-a",
            "priority_map": {"normal": "normal"},
        }
    )
    service.register("scheduling_profile", SchedulingProfile.model_validate(body), "team-a")
    with service.store.transaction() as conn:
        result = compile_plan(
            service,
            conn,
            "team-a",
            SchedulingPlanRequest(profile_ref=body["ref"], workload_ref=spec.ref),
        )
    assert result["plan"]["backend"] == "slurm"
    job = service.submit(
        "team-a",
        JobRequest(
            workload_ref=spec.ref,
            candidate_ref="hpc",
            scheduling_profile_ref=body["ref"],
            scheduling_plan_digest=result["digest"],
        ),
        "hpc-policy",
    )
    with service.store.transaction() as conn:
        row = dict(service.store.job(conn, job["job_id"]))
    backend = SlurmBackend(
        partition="compute",
        account="team-a",
        qos="normal",
        output_dir="/tmp/ra-test",
        native_runtimes=native_runtime(row),
    )
    script = backend.script(row)
    assert "#SBATCH --qos=normal" in script
    assert "#SBATCH --account=team-a" in script
    backend.qos = "changed"
    with pytest.raises(BackendError, match="SCHEDULING_BINDING_DRIFT"):
        backend.script(row)


@pytest.mark.parametrize("headroom,expected", [(1, "spare"), (0, "base")])
def test_automatic_job_selects_fresh_capacity_and_queues_when_busy(
    service, bundle, headroom, expected
):
    from test_worker import SchedulerDouble

    from resource_advisor.contracts import JobTemplate, now
    from resource_advisor.inventory import save_inventory, signal
    from resource_advisor.worker import Worker

    spec, candidate, variant, cap = bundle
    spare_cap = cap.model_copy(update={"ref": "spare-cap", "node_ref": "spare-node"})
    spare = candidate.model_copy(update={"ref": "spare", "capability_ref": spare_cap.ref})
    service.operational_mode = True
    spec = spec.model_copy(update={"ref": "auto-work", "candidates": (candidate, spare)})
    variant = variant.model_copy(update={"ref": "auto-variant", "workload_ref": spec.ref})
    spec = spec.model_copy(
        update={
            "candidates": tuple(
                c.model_copy(update={"variant_ref": variant.ref}) for c in spec.candidates
            )
        }
    )
    for kind, model in [
        ("capability", spare_cap),
        ("variant", variant),
        ("workload", spec),
        ("scheduling_profile", profile()),
    ]:
        service.register(kind, model, "team-a")
    observed = now().isoformat()

    def node(ref, free):
        return {
            "node_ref": ref,
            "ready": signal(True, observed_at=observed, source="fixture"),
            "resources": {
                key: {"request_headroom": signal(value, observed_at=observed, source="fixture")}
                for key, value in {"cpu": 2, "memory": 2**30, "nvidia.com/gpu": free}.items()
            },
        }

    save_inventory(
        service.store,
        "team-a",
        {
            "ref": "inventory-fixture",
            "cluster_ref": "lab",
            "collected_at": observed,
            "stale_after_seconds": 120,
            "status": "ok",
            "nodes": [node(cap.node_ref, 0), node(spare_cap.node_ref, headroom)],
        },
    )
    service.register_template(
        "team-a",
        JobTemplate(
            ref="auto-template",
            name="Auto",
            workload_ref=spec.ref,
            max_run_seconds=30,
            max_queue_seconds=90,
        ),
    )
    request = JobRequest(
        workload_ref=spec.ref, scheduling_profile_ref="interactive-v1", template_ref="auto-template"
    )
    job = service.submit("team-a", request, "automatic")
    # Reusing the same request never selects or submits a second job.
    assert service.submit("team-a", request, "automatic")["job_id"] == job["job_id"]
    with service.store.transaction() as conn:
        saved = service.store.job(conn, job["job_id"])["body"]
    assert saved["candidate"]["ref"] == expected
    assert len(saved["scheduling_plan"]["evaluation"]) == 2
    backend = SchedulerDouble()
    assert Worker(service, {("team-a", "lab"): backend}).submit_one()
    assert service.get_job("team-a", job["job_id"])["state"] == "QUEUED"


def test_stale_capacity_does_not_claim_available(bundle):
    from resource_advisor.scheduling import availability

    _, candidate, _, cap = bundle
    observations = {
        ("kubernetes", cap.node_ref): {
            "snapshot_ref": "old",
            "collected_at": "old",
            "node": {
                "resources": {
                    "nvidia.com/gpu": {"request_headroom": {"status": "stale", "value": 10}}
                }
            },
        }
    }
    assert availability(candidate, cap, observations)["status"] == "unknown"


def test_common_pool_selection_accounts_for_accepted_burst_demand(service, bundle):
    spec, candidate, variant, cap = bundle
    second_cap = cap.model_copy(update={"ref": "second-cap", "node_ref": "second-node"})
    second = candidate.model_copy(update={"ref": "second", "capability_ref": second_cap.ref})
    variant = variant.model_copy(update={"ref": "pool-variant", "workload_ref": "pool-work"})
    spec = spec.model_copy(
        update={
            "ref": "pool-work",
            "candidates": tuple(
                c.model_copy(update={"variant_ref": variant.ref}) for c in (candidate, second)
            ),
        }
    )
    service.operational_mode = True
    for kind, model in [
        ("capability", second_cap),
        ("variant", variant),
        ("workload", spec),
        ("scheduling_profile", profile()),
    ]:
        service.register(kind, model, "team-a")
    request = JobRequest(workload_ref=spec.ref, scheduling_profile_ref="interactive-v1")
    first = service.submit("team-a", request, "pool-1")
    second = service.submit("team-a", request, "pool-2")
    with service.store.transaction() as conn:
        a = service.store.job(conn, first["job_id"])["body"]
        b = service.store.job(conn, second["job_id"])["body"]
    assert a["capability"]["node_ref"] != b["capability"]["node_ref"]
    assert service.submit("team-a", request, "pool-1")["job_id"] == first["job_id"]
    assert any(
        e["availability"]["accepted_active_jobs"] == 1 for e in b["scheduling_plan"]["evaluation"]
    )
