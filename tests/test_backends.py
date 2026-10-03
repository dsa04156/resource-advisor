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


def native_runtime(job):
    job["body"]["variant"]["image"] = None
    return {
        job["body"]["variant"]["ref"]: {
            "environment_digest": job["body"]["variant"]["environment_digest"],
            "guard_path": "/opt/ra/runtime_guard.py",
            "guard_digest": "sha256:" + "1" * 64,
            "manifest_path": "/opt/ra/runtime.json",
            "manifest_digest": "sha256:" + "2" * 64,
            "commands": [job["body"]["effective_command"]],
        }
    }


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
    backend = SlurmBackend(
        partition="gpu",
        account="team-a",
        qos="lab",
        output_dir="/tmp/ra-test",
        native_runtimes=native_runtime(job),
    )
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
            assert "--jobs" not in args  # Purged completed IDs must reach sacct.
            assert args[args.index("--account") + 1] == "team-a"
            return ""
        assert args[:4] == ["env", "TZ=UTC", "SLURM_TIME_FORMAT=%Y-%m-%dT%H:%M:%S%z", "sacct"]
        return (
            f"15|{job['body']['attempt_id']}|team-a|gpu|COMPLETED|0:0|"
            "2026-10-02T05:00:00+0000|2026-10-02T05:00:02+0000||Unknown"
        )

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
        native_runtimes=native_runtime(job),
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


@pytest.mark.parametrize("retention", [False, True])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_terminated_container_waits_for_job_condition_without_requeue(
    service, retention, exit_code
):
    from test_kubernetes_retention import Scheduler

    job = row(service)
    job["body"]["external_id"] = job["body"]["attempt_id"]
    scheduler = Scheduler(job)
    scheduler.terminate(exit_code)
    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        retain_termination_evidence=retention,
        execute=scheduler.execute,
    )
    observation = backend.status(job)
    assert observation.state == "RUNNING"
    assert observation.started_at == "2026-10-03T00:00:01Z"
    assert observation.execution_started_at == "2026-10-03T00:00:03Z"
    assert observation.finished_at is None  # No early backend completion/collection.
    assert observation.allocation["accelerator_count"] == 1
    scheduler.obj["status"]["conditions"] = [
        {"type": "Failed" if exit_code else "Complete", "status": "True"}
    ]
    terminal = backend.status(job)
    assert terminal.state == ("FAILED" if exit_code else "COLLECTING")
    assert terminal.finished_at == "2026-10-03T00:00:08Z"


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


def test_slurm_cannot_silently_ignore_a_container_image(service):
    job = row(service)
    backend = SlurmBackend(partition="gpu", account="a", qos="normal", output_dir="/tmp/ra")
    with pytest.raises(BackendError, match="container execution"):
        backend.validate(job)
    job["body"]["variant"]["image"] = None
    with pytest.raises(BackendError, match="native runtime binding"):
        backend.validate(job)


@pytest.mark.parametrize("wrong", [None, "account", "node", "attempt", "partition", "duplicate"])
def test_slurm_reads_node_local_result_only_after_accounting_match(service, wrong):
    job = row(service)
    job["body"]["external_id"] = "42"
    job["body"]["capability"]["node_ref"] = "qualified-node"
    attempt = job["body"]["attempt_id"]
    fields = ["42", attempt, "team-a", "gpu", "qualified-node", "COMPLETED", "0:0"]
    if wrong in {"account", "node", "attempt", "partition"}:
        fields[{"attempt": 1, "account": 2, "partition": 3, "node": 4}[wrong]] = "foreign"
    commands = []

    def execute(args, **kwargs):
        commands.append(args)
        if "sacct" in args[-1]:
            record = "|".join(fields) + "\n"
            return record * (2 if wrong == "duplicate" else 1)
        assert args[-2] == "lab-worker-results"
        assert "StrictHostKeyChecking=yes" in args
        assert args[-1] == f"tail -c 65537 -- /tmp/ra/{attempt}.log"
        return 'RESOURCE_ADVISOR_RESULT {"result": "test-envelope"}\n'

    backend = SlurmBackend(
        partition="gpu",
        account="team-a",
        qos="normal",
        output_dir="/tmp/ra",
        ssh_target="lab-controller",
        result_ssh_targets={"qualified-node": "lab-worker-results"},
        execute=execute,
    )
    if wrong:
        with pytest.raises(BackendError):
            backend.result(job)
        assert len(commands) == 1  # No log read on any unverified node.
    else:
        assert backend.result(job) == {"result": "test-envelope"}
        assert commands[0][-2] == "lab-controller"


def test_slurm_response_loss_reconciliation_does_not_attach_other_account(service):
    job = row(service)
    attempt = job["body"]["attempt_id"]

    def execute(args, **kwargs):
        assert "team-a" in args
        # Deliberately include another account despite the requested filter.
        return f"42|{attempt}|foreign|gpu\n43|{attempt}|team-a|gpu\n44|{attempt}|team-a|foreign\n"

    backend = SlurmBackend(
        partition="gpu", account="team-a", qos="normal", output_dir="/tmp/ra", execute=execute
    )
    assert backend.reconcile(job) == "43"


def test_kubernetes_queue_time_uses_server_creation_not_submit_response(service):
    from resource_advisor.accounting import ledger_record

    job = row(service)
    job["body"]["external_id"] = "attempt"
    job["body"]["queued_at"] = "2026-10-02T08:00:00.800000+00:00"

    def execute(args, **kwargs):
        if "pods" in args:
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"ownerReferences": [{"uid": "u1"}]},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "PodScheduled",
                                        "status": "True",
                                        "lastTransitionTime": "2026-10-02T08:00:00Z",
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
        return json.dumps({"metadata": {"uid": "u1", "creationTimestamp": "2026-10-02T07:59:59Z"}})

    backend = KubernetesBackend(
        namespace="research-a", local_queue="batch", node_selector={"pool": "lab"}, execute=execute
    )
    observation = backend.status(job)
    assert observation.submitted_at == "2026-10-02T07:59:59Z"
    body = dict(
        job["body"],
        scheduler_submitted_at=observation.submitted_at,
        started_at=observation.started_at,
    )
    record = ledger_record(job, "FAILED", body)
    assert record["queue_seconds"] == 1
    assert record["body"]["submission_time_source"] == "scheduler"
    assert "QUEUE_WHOLE_SECOND_RESOLUTION" in record["body"]["uncertainty"]
