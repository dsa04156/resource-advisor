import json

import pytest

from resource_advisor.backends import BackendError, KubernetesBackend, SlurmBackend, result_from_log
from resource_advisor.contracts import JobRequest


def row(service):
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "one"
    )
    with service.store.transaction() as conn:
        return dict(service.store.job(conn, job["job_id"]))


def test_kubernetes_submission_suspended_and_bounded(service):
    job = row(service)
    obj = KubernetesBackend(
        namespace="research-a", local_queue="batch", node_selector={"pool": "lab"}
    ).manifest(job)
    assert obj["spec"]["suspend"] is True
    pod = obj["spec"]["template"]["spec"]
    assert "nodeName" not in pod
    assert pod["nodeSelector"]["pool"] == "lab"
    assert pod["automountServiceAccountToken"] is False
    resources = pod["containers"][0]["resources"]
    assert resources["requests"]["nvidia.com/gpu"] == "1"
    assert resources["limits"] == resources["requests"]
    assert obj["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "batch"


def test_qualified_runtime_mount_is_read_only_and_digest_bound(service):
    job = row(service)
    bundle = {
        "variant-1": {
            "environment_digest": job["body"]["variant"]["environment_digest"],
            "pvc": "qualified-packages",
            "source_config_map": "immutable-source",
        }
    }
    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        runtime_bundles=bundle,
    )
    pod = backend.manifest(job)["spec"]["template"]["spec"]
    assert all(m["readOnly"] for m in pod["containers"][0]["volumeMounts"])
    bundle["variant-1"]["environment_digest"] = "wrong"
    with pytest.raises(BackendError, match="qualified environment"):
        backend.manifest(job)


def test_slurm_is_not_wrapped_in_kueue_and_quotes_payload(service):
    job = row(service)
    job["body"]["capability"]["resource_key"] = "gpu:test"
    job["body"]["variant"]["command"] = ["python", "runner.py", "$(touch /tmp/not-executed)"]
    job["body"]["effective_command"] = job["body"]["variant"]["command"]
    backend = SlurmBackend(partition="gpu", account="team-a", qos="lab", output_dir="/tmp/ra-test")
    script = backend.script(job)
    assert "#SBATCH --gres=gpu:test:1" in script
    assert "#SBATCH --account=team-a" in script
    assert "'$(touch /tmp/not-executed)'" in script
    assert "kueue" not in script


def test_slurm_accounting_requests_explicit_timezone(service):
    job = row(service)
    job["body"]["external_id"] = "15"

    def execute(args, **_):
        if args[0] == "squeue":
            return ""
        assert args[:4] == ["env", "TZ=UTC", "SLURM_TIME_FORMAT=%Y-%m-%dT%H:%M:%S%z", "sacct"]
        return "15|COMPLETED|0:0|2026-10-02T05:00:00+0000|2026-10-02T05:00:02+0000"

    backend = SlurmBackend(
        partition="gpu", account="team-a", qos="lab", output_dir="/tmp/ra-test", execute=execute
    )
    result = backend.status(job)
    assert result.state == "COLLECTING"
    assert result.started_at.endswith("+0000")
    assert result.finished_at.endswith("+0000")


def test_priority_grade_maps_to_distinct_backend_policies(service):
    job = row(service)
    job["body"]["spec"]["execution"]["priority"] = "high"
    k8s = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        priority_classes={"high": "research-urgent"},
    )
    manifest = k8s.manifest(job)
    assert manifest["metadata"]["labels"]["kueue.x-k8s.io/priority-class"] == "research-urgent"
    assert "priorityClassName" not in manifest["spec"]["template"]["spec"]
    job["body"]["capability"]["resource_key"] = "gpu:test"
    slurm = SlurmBackend(
        partition="gpu",
        account="team-a",
        qos="standard",
        output_dir="/tmp/ra-test",
        qos_by_priority={"high": "lab-expedited"},
    )
    assert "#SBATCH --qos=lab-expedited" in slurm.script(job)
    assert "--priority=" not in slurm.script(job)


@pytest.mark.parametrize("kind", ["kubernetes", "slurm"])
def test_unmapped_priority_is_rejected_before_submission(service, kind):
    job = row(service)
    job["body"]["spec"]["execution"]["priority"] = "high"
    job["body"]["capability"]["resource_key"] = "gpu:test"
    if kind == "slurm":
        backend = SlurmBackend(partition="gpu", account="a", qos="normal", output_dir="/tmp/ra")
        build = backend.script
    else:
        backend = KubernetesBackend(namespace="a", local_queue="batch", node_selector={"pool": "a"})
        build = backend.manifest
    with pytest.raises(BackendError, match="priority is not enabled"):
        build(job)


@pytest.mark.parametrize("text", ["", "RESOURCE_ADVISOR_RESULT {}\nRESOURCE_ADVISOR_RESULT {}"])
def test_missing_or_duplicate_envelope_rejected(text):
    with pytest.raises(BackendError):
        result_from_log(text)


def test_reconciliation_rejects_foreign_kubernetes_job(service):
    job = row(service)
    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        execute=lambda *_args, **_kwargs: json.dumps(
            {"metadata": {"name": job["body"]["attempt_id"], "annotations": {}}}
        ),
    )
    with pytest.raises(BackendError, match="does not match"):
        backend.reconcile(job)


def test_pending_pod_is_not_running_even_when_job_active(service):
    job = row(service)
    job["body"]["external_id"] = "attempt"

    def execute(args, **_kwargs):
        if "pods" in args:
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"ownerReferences": [{"uid": "u1"}]},
                            "status": {"phase": "Pending"},
                        }
                    ]
                }
            )
        return json.dumps({"metadata": {"uid": "u1"}, "status": {"active": 1}})

    backend = KubernetesBackend(
        namespace="research-a", local_queue="batch", node_selector={"pool": "lab"}, execute=execute
    )
    assert backend.status(job).state == "QUEUED"


def test_deleted_job_with_running_pod_is_not_confirmed_cancel(service):
    job = row(service)
    job["body"]["external_id"] = "attempt"
    job["state"] = "CANCEL_REQUESTED"

    def execute(args, **_kwargs):
        return json.dumps({"items": [{"status": {"phase": "Running"}}]}) if "pods" in args else ""

    backend = KubernetesBackend(
        namespace="research-a", local_queue="batch", node_selector={"pool": "lab"}, execute=execute
    )
    assert backend.status(job).state == "CANCEL_REQUESTED"
