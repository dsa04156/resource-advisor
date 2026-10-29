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


def test_versioned_variant_explicitly_reuses_approved_runtime_bundle(service):
    job = row(service)
    job["body"]["variant"].update(
        ref="variant-v2", kubernetes_runtime_bundle_ref="approved-runtime"
    )
    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        runtime_bundles={
            "approved-runtime": {
                "environment_digest": job["body"]["variant"]["environment_digest"],
                "pvc": "qualified-packages",
                "source_config_map": "immutable-source",
            }
        },
    )
    pod = backend.manifest(job)["spec"]["template"]["spec"]
    assert pod["volumes"][0]["persistentVolumeClaim"] == {
        "claimName": "qualified-packages",
        "readOnly": True,
    }
    assert all(m["readOnly"] for m in pod["containers"][0]["volumeMounts"])
    assert {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}["PYTHONPATH"] == (
        "/opt/resource-advisor:/opt/qualified-runtime/site"
    )


@pytest.mark.parametrize("configured", [False, True])
def test_required_runtime_bundle_fails_preflight_without_native_submission(service, configured):
    job = row(service)
    job["body"]["variant"]["kubernetes_runtime_bundle_ref"] = "approved-runtime"
    calls = []
    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        runtime_bundles={"approved-runtime": {"environment_digest": "wrong"}} if configured else {},
        execute=lambda *args, **kwargs: calls.append(args),
    )
    with pytest.raises(BackendError, match="qualified"):
        backend.validate(job)
    assert calls == []


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


@pytest.mark.parametrize(
    "cpu_only,extra_request,expected",
    [
        (True, None, 0.0),
        (False, None, None),
        (True, ("nvidia.com/gpu", "1"), None),
    ],
)
def test_zero_accelerator_allocation_requires_observed_cpu_only_requests(
    service, cpu_only, extra_request, expected
):
    from test_kubernetes_retention import Scheduler

    job = row(service)
    job["body"]["external_id"] = job["body"]["attempt_id"]
    if cpu_only:
        job["body"]["capability"].update(device_class="cpu", resource_key=None)
        context = job["body"]["candidate"]["context"]
        context["allocation_mode"] = "cpu_only"
        context["resources"]["accelerator_count"] = 0
    scheduler = Scheduler(job)
    requests = scheduler.pod["spec"]["containers"][0]["resources"]["requests"]
    requests.pop("nvidia.com/gpu")
    if extra_request:
        requests[extra_request[0]] = extra_request[1]
    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        execute=scheduler.execute,
    )
    observation = backend.status(job)
    assert observation.allocation["accelerator_count"] == expected
    assert observation.allocation["cpu"] == 0.5


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


def test_host_network_requires_operator_variant_and_environment_binding(service):
    job = row(service)
    job["body"]["candidate"]["context"]["runtime_versions"]["transport_mode"] = "host-persistent"
    options = dict(namespace="research-a", local_queue="batch", node_selector={"pool": "lab"})
    with pytest.raises(BackendError, match="host network"):
        KubernetesBackend(**options).manifest(job)
    with pytest.raises(BackendError, match="host network"):
        KubernetesBackend(
            **options, host_network_variants={"variant-1": "sha256:" + "0" * 64}
        ).manifest(job)
    backend = KubernetesBackend(
        **options, host_network_variants={"variant-1": job["body"]["variant"]["environment_digest"]}
    )
    pod = backend.manifest(job)["spec"]["template"]["spec"]
    assert pod["hostNetwork"] is True
    assert pod["dnsPolicy"] == "ClusterFirstWithHostNet"
    assert pod["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"


def test_default_route_keeps_pod_network_and_dedicated_transport_rejects_legacy(service):
    job = row(service)
    options = dict(namespace="research-a", local_queue="batch", node_selector={"pool": "lab"})
    assert (
        "hostNetwork" not in KubernetesBackend(**options).manifest(job)["spec"]["template"]["spec"]
    )
    with pytest.raises(BackendError, match="host network"):
        KubernetesBackend(
            **options, host_network_variants={"different-variant": "sha256:" + "0" * 64}
        ).manifest(job)


def kueue_workload(job_uid, queue="batch", *, admitted):
    conditions = [
        {
            "type": "QuotaReserved",
            "status": "True" if admitted else "False",
            "reason": "QuotaReserved" if admitted else "Pending",
            "message": "couldn't assign flavors to pod set main: insufficient unused quota"
            if not admitted
            else "Quota reserved in ClusterQueue ra-live",
            "lastTransitionTime": "2026-10-28T01:00:00Z",
        }
    ]
    if admitted:
        conditions.append(
            {
                "type": "Admitted",
                "status": "True",
                "reason": "Admitted",
                "message": "The workload is admitted",
                "lastTransitionTime": "2026-10-28T01:00:01Z",
            }
        )
    return {
        "metadata": {
            "name": "job-attempt-1a2b3",
            "uid": "wl-uid",
            "labels": {"kueue.x-k8s.io/job-uid": job_uid},
            "ownerReferences": [{"kind": "Job", "uid": job_uid, "controller": True}],
        },
        "spec": {"queueName": queue},
        "status": {
            "conditions": conditions,
            **(
                {
                    "admission": {
                        "clusterQueue": "ra-live",
                        "podSetAssignments": [
                            {
                                "name": "main",
                                "flavors": {"cpu": "ra-live-cpu"},
                                "resourceUsage": {"cpu": "2", "memory": "1Gi"},
                            }
                        ],
                    }
                }
                if admitted
                else {}
            ),
        },
    }


@pytest.mark.parametrize("admitted", [False, True])
def test_kubernetes_observation_records_owned_pod_node_and_kueue_admission(service, admitted):
    from test_kubernetes_retention import Scheduler

    job = row(service)
    job["body"]["external_id"] = job["body"]["attempt_id"]
    scheduler = Scheduler(job)
    scheduler.pod["spec"]["nodeName"] = "etri-node-1"
    if not admitted:
        scheduler.obj["spec"] = {"suspend": True}
        scheduler.pod = None
    foreign = kueue_workload("other-job-uid", admitted=True)
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if "workloads.kueue.x-k8s.io" in args:
            return json.dumps({"items": [kueue_workload("job-uid", admitted=admitted), foreign]})
        if "pods" in args:
            return json.dumps({"items": [scheduler.pod] if scheduler.pod else []})
        return json.dumps(scheduler.obj)

    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        observe_admission=True,
        execute=execute,
    )
    observation = backend.status(job)
    query = next(a for a in calls if "workloads.kueue.x-k8s.io" in a)
    assert "kueue.x-k8s.io/job-uid=job-uid" in query and "--namespace" in query
    admission = observation.admission
    assert admission["source"] == "kueue:Workload" and admission["uid"] == "wl-uid"
    assert admission["local_queue"] == "batch"
    reserved = next(c for c in admission["conditions"] if c["type"] == "QuotaReserved")
    if admitted:
        assert observation.state == "RUNNING"
        assert admission["cluster_queue"] == "ra-live"
        assert admission["resource_usage"] == {"cpu": "2", "memory": "1Gi"}
        assert observation.placement == {
            "source": "kubernetes:Pod",
            "job_uid": "job-uid",
            "pod_name": "attempt-pod",
            "pod_uid": "pod-uid",
            "node_name": "etri-node-1",
            "phase": "Running",
        }
    else:
        assert observation.state == "QUEUED" and observation.placement is None
        assert observation.scheduler_reason == "AdmissionPending"
        assert reserved["status"] == "False" and "insufficient" in reserved["message"]


def test_kubernetes_admission_read_failure_keeps_scheduler_state(service):
    from test_kubernetes_retention import Scheduler

    job = row(service)
    job["body"]["external_id"] = job["body"]["attempt_id"]
    scheduler = Scheduler(job)

    def execute(args, **kwargs):
        if "workloads.kueue.x-k8s.io" in args:
            raise BackendError("kubectl exited with code 1")
        if "pods" in args:
            return json.dumps({"items": [scheduler.pod]})
        return json.dumps(scheduler.obj)

    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        observe_admission=True,
        execute=execute,
    )
    observation = backend.status(job)
    assert observation.state == "RUNNING"
    assert observation.admission == {"source": "kueue:Workload", "error": "UNAVAILABLE"}


def test_progress_parser_keeps_owned_bounded_records_only():
    from resource_advisor.backends import progress_from_log

    def record(attempt, epoch, loss=0.5):
        return "RA_PROGRESS " + json.dumps(
            {
                "schema": "ra-progress-v1",
                "job_id": "j-1",
                "attempt_id": attempt,
                "epoch": epoch,
                "total_epochs": 50,
                "optimizer_steps": epoch * 4,
                "loss": loss,
                "elapsed_seconds": epoch * 1.5,
                "observed_at": "2026-10-28T01:00:00+00:00",
                "injected": "<script>",
            }
        )

    log = "\n".join(
        [record("a-1", n) for n in range(30)]
        + [record("a-other", 99), "x" * 5000, "RESOURCE_ADVISOR_RESULT {" + "1" * 9000 + "}"]
    )
    view = progress_from_log(log, {"id": "j-1", "body": {"attempt_id": "a-1"}}, "fixture")
    assert view["attempt_id"] == "a-1" and view["source"] == "fixture"
    assert view["latest"]["epoch"] == 29 and "injected" not in view["latest"]
    assert [r["epoch"] for r in view["records"]] == list(range(10, 30))
    assert len(view["log_tail"]) <= 40 and all(len(line) <= 300 for line in view["log_tail"])
    assert view["log_tail"][-1] == "[result envelope]"
    assert view["observed_at"]
    nan = record("a-1", 31).replace('"loss": 0.5', '"loss": NaN')
    assert progress_from_log(nan, {"id": "j-1", "body": {"attempt_id": "a-1"}}, "x")["latest"] is None


def test_kubernetes_progress_reads_bounded_workload_log(service):
    job = row(service)
    job["body"]["external_id"] = job["body"]["attempt_id"]
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return "starting\n"

    backend = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        observe_progress=True,
        execute=execute,
    )
    view = backend.progress(job, None)
    assert calls[-1][-4:] == ["job/" + job["body"]["attempt_id"], "--container", "workload", "--tail=60"]
    assert view["log_tail"] == ["starting"] and view["latest"] is None


def test_slurm_progress_reads_owned_output_through_node_transport(service):
    from resource_advisor.backends import Observation

    job = row(service)
    job["body"]["external_id"] = "77"
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return "epoch log\n"

    backend = SlurmBackend(
        partition="compute",
        account="ra-project",
        qos="ra-normal",
        output_dir="/srv/ra/results",
        ssh_target="controller",
        result_ssh_targets={"vm-node": "vm-results"},
        observe_progress=True,
        execute=execute,
    )
    running = Observation("RUNNING", placement={"source": "slurm:squeue", "node_name": "vm-node"})
    assert backend.progress(job, running)["log_tail"] == ["epoch log"]
    assert calls[-1][-2:] == [
        "vm-results",
        "tail -c 16384 -- /srv/ra/results/" + job["body"]["attempt_id"] + ".log",
    ]
    with pytest.raises(BackendError):
        backend.progress(job, Observation("RUNNING", placement={"node_name": "other-node"}))


@pytest.mark.parametrize("state", ["PENDING", "RUNNING"])
def test_slurm_live_observation_reports_reason_node_and_running_allocation(service, state):
    job = row(service)
    job["body"]["external_id"] = "81"
    attempt = job["body"]["attempt_id"]
    calls = []

    def execute(args, **_):
        calls.append(args)
        if args[0] == "squeue":
            assert args[-1] == "--format=%i|%j|%a|%P|%T|%r|%N"
            node = "(Resources)" if state == "PENDING" else "vm-node"
            reason = "Resources" if state == "PENDING" else "None"
            return (
                f"80|other|ra-project|compute|RUNNING|None|vm-node\n"
                f"81|{attempt}|ra-project|compute|{state}|{reason}|{node}\n"
            )
        assert args[3] == "sacct" and "--jobs" in args and args[-2] == "81"
        return (
            f"81|{attempt}|ra-project|compute|RUNNING|0:0|2026-10-28T01:00:05+0000|Unknown|"
            "billing=2,cpu=2,mem=1024M,node=1|2026-10-28T01:00:00+0000\n"
        )

    backend = SlurmBackend(
        partition="compute",
        account="ra-project",
        qos="ra-normal",
        output_dir="/srv/ra/results",
        observe_allocation=True,
        execute=execute,
    )
    observation = backend.status(job)
    if state == "PENDING":
        assert observation.state == "QUEUED" and observation.scheduler_reason == "Resources"
        assert observation.placement is None and observation.allocation is None
        assert len(calls) == 1
    else:
        assert observation.state == "RUNNING" and observation.scheduler_reason is None
        assert observation.started_at == "2026-10-28T01:00:05+0000"
        assert observation.submitted_at == "2026-10-28T01:00:00+0000"
        assert observation.allocation == {
            "source": "slurm:sacct AllocTRES",
            "accelerator_count": None,
            "cpu": 2.0,
            "memory_mib": 1024.0,
        }
        assert observation.placement == {
            "source": "slurm:squeue",
            "job_id": "81",
            "node_name": "vm-node",
            "state": "RUNNING",
        }
