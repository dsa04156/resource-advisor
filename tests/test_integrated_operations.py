"""Integrated lifecycle, usage and policy checks with explicit scheduler doubles.

Doubles inject faults at the native boundary; nothing here is scheduler evidence.
"""

import hashlib
import importlib.util
import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_worker import SchedulerDouble, expire_retry

from resource_advisor.accounting import month_period, period_report
from resource_advisor.api import Principal, create_app
from resource_advisor.backends import (
    BackendError,
    KubernetesBackend,
    Observation,
    SlurmBackend,
    SubmissionAmbiguous,
    SubmissionRejected,
    native_rejection,
)
from resource_advisor.configuration import ConfigurationError, principals
from resource_advisor.contracts import JobRequest, State, now
from resource_advisor.service import Rejected
from resource_advisor.store import Conflict, outbox, usage
from resource_advisor.submission_lifecycle import classify_wait
from resource_advisor.worker import Worker

PREFIX = "/api/v1/compute"


class NativeDouble(SchedulerDouble):
    """Reconcile answers are scripted per call: an ID, None, or an exception."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.answers = []
        self.reconciles = 0

    def reconcile(self, job):
        self.reconciles += 1
        answer = self.answers.pop(0) if self.answers else self.external_id
        if isinstance(answer, Exception):
            raise answer
        return answer


def submit(service, key="one", project="team-a"):
    return service.submit(project, JobRequest(workload_ref="workload-1", candidate_ref="base"), key)


def pre_call_crash(service):
    """Intent committed, process died before the native call; lease expired."""
    backend = NativeDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    job = submit(service)
    event = service.store.claim("submit", job_routes=worker.backends)
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.SUBMITTING, row["body"])
    with service.store.transaction() as conn:
        conn.execute(
            update(outbox)
            .where(outbox.c.id == event["id"])
            .values(lease_until=(now() - timedelta(seconds=1)).isoformat())
        )
    return backend, worker, job


def cycle(service, worker):
    expire_retry(service)
    worker.submit_one()
    return service.get_job("team-a", worker_job(service))


def worker_job(service):
    with service.store.transaction() as conn:
        return conn.execute(select(outbox.c.body)).scalars().first()["job_id"]


def body(service, job_id):
    with service.store.transaction() as conn:
        return service.store.job(conn, job_id)


def ledger(service):
    with service.store.transaction() as conn:
        return list(conn.execute(select(usage)).mappings())


def test_idempotent_request_keeps_one_logical_job_and_attempt(service):
    first, again = submit(service), submit(service)
    assert (first["job_id"], first["attempt_id"]) == (again["job_id"], again["attempt_id"])
    with pytest.raises(Conflict):
        service.submit("team-a", JobRequest(workload_ref="workload-1", candidate_ref="x"), "one")


def test_lost_response_with_native_job_adopts_once_and_records_match(service):
    backend = NativeDouble(lose_response=True)
    worker = Worker(service, {("team-a", "lab"): backend})
    job = submit(service)
    worker.submit_one()
    assert service.get_job("team-a", job["job_id"])["state"] == "SUBMISSION_UNKNOWN"
    expire_retry(service)
    worker.submit_one()
    current = body(service, job["job_id"])
    assert current["state"] == "QUEUED" and current["body"]["external_id"] == "42"
    assert current["body"]["submission_reconciliation"]["last_outcome"] == "MATCHED"
    assert backend.submissions == 1
    assert submit(service)["attempt_id"] == job["attempt_id"]


def test_pre_call_crash_is_uncertain_and_never_resubmitted(service):
    backend, worker, job = pre_call_crash(service)
    backend.external_id = None
    for _ in range(3):
        cycle(service, worker)
    current = body(service, job["job_id"])
    assert current["state"] == "SUBMISSION_UNKNOWN"
    assert current["body"]["submission_reconciliation"]["consecutive_no_match"] == 3
    assert backend.submissions == 0
    assert ledger(service) == []


def acknowledge(service, job, **change):
    args = {"decision": "ACKNOWLEDGE_UNRESOLVED", "reason": "sacct/squeue show no attempt"}
    return service.resolve_submission(
        "team-a", job["job_id"], **{**args, "subject": "op", **change}
    )


def test_acknowledgement_needs_evidence_and_keeps_native_state_open(service):
    backend, worker, job = pre_call_crash(service)
    backend.external_id = None
    cycle(service, worker)
    with pytest.raises(Rejected, match="INSUFFICIENT_NO_MATCH_EVIDENCE"):
        acknowledge(service, job)
    cycle(service, worker)
    acknowledged = acknowledge(service, job)
    assert acknowledged["state"] == "SUBMISSION_UNKNOWN"
    assert acknowledged["submission_resolution"]["status"] == "ACKNOWLEDGED"
    worker.submit_one()  # One fresh reconcile without waiting for backoff.
    for _ in range(2):
        cycle(service, worker)
    current = body(service, job["job_id"])
    # No-match never proves absence: not terminal, no usage row, no zero claim.
    assert current["state"] == "SUBMISSION_UNKNOWN"
    assert current["body"]["submission_reconciliation"]["checks"] == 5
    assert backend.submissions == 0 and ledger(service) == []
    with service.store.transaction() as conn:
        audit = service.store.get(conn, "submission_resolution", job["attempt_id"])
        event = conn.execute(select(outbox.c.status)).scalar_one()
    assert audit["body"]["requested_by"] == "op"
    assert audit["body"]["evidence"]["consecutive_no_match"] == 2
    assert event == "PENDING"  # Reconciliation continues; nothing resubmits.


def test_late_native_match_after_acknowledgement_is_adopted(service):
    backend, worker, job = pre_call_crash(service)
    backend.external_id = None
    cycle(service, worker)
    cycle(service, worker)
    acknowledge(service, job)
    for _ in range(3):
        cycle(service, worker)
    backend.external_id = "77"  # E.g. delayed accounting visibility.
    cycle(service, worker)
    current = body(service, job["job_id"])
    assert current["state"] == "QUEUED" and current["body"]["external_id"] == "77"
    assert current["body"]["submission_resolution"]["status"] == "SUPERSEDED_BY_NATIVE_MATCH"
    assert backend.submissions == 0
    backend.observation = Observation(State.FAILED, error="NODE_FAIL")
    worker.reconcile_all()
    [record] = ledger(service)
    assert record["body"]["outcome"] == "FAILED"


@pytest.mark.parametrize(
    "fault, outcome",
    [(BackendError("ssh exited 255"), "QUERY_FAILED"), (SubmissionAmbiguous("2"), "AMBIGUOUS")],
)
def test_failed_or_ambiguous_query_resets_evidence(service, fault, outcome):
    backend, worker, job = pre_call_crash(service)
    backend.answers = [None, None, fault]
    for _ in range(3):
        cycle(service, worker)
    record = body(service, job["job_id"])["body"]["submission_reconciliation"]
    assert record["last_outcome"] == outcome and record["consecutive_no_match"] == 0
    with pytest.raises(Rejected, match="INSUFFICIENT_NO_MATCH_EVIDENCE"):
        acknowledge(service, job)


def test_cancel_of_uncertain_submission_never_claims_confirmation(service):
    backend, worker, job = pre_call_crash(service)
    backend.external_id = None
    cycle(service, worker)
    service.cancel("team-a", job["job_id"])
    for _ in range(2):
        expire_retry(service)
        worker.cancel_one()
    current = body(service, job["job_id"])
    assert current["state"] == "CANCEL_REQUESTED"
    assert current["body"]["submission_reconciliation"]["consecutive_no_match"] == 3
    acknowledge(service, job, reason="user canceled; no native job found")
    worker.cancel_one()
    assert body(service, job["job_id"])["state"] == "CANCEL_REQUESTED"
    assert backend.cancel_calls == 0 and ledger(service) == []
    backend.external_id = "88"
    backend.observation = Observation(State.CANCELED)
    expire_retry(service)
    worker.cancel_one()
    current = body(service, job["job_id"])
    assert current["state"] == "CANCELED" and current["body"]["external_id"] == "88"
    [record] = ledger(service)
    assert record["body"]["never_submitted"] is False


def test_resolution_does_not_steal_a_leased_event(service):
    backend, worker, job = pre_call_crash(service)
    backend.external_id = None
    cycle(service, worker)
    cycle(service, worker)
    expire_retry(service)
    leased = service.store.claim("submit", job_routes=worker.backends)
    acknowledge(service, job)
    with service.store.transaction() as conn:
        row = conn.execute(select(outbox).where(outbox.c.id == leased["id"])).mappings().one()
    assert row["status"] == "LEASED" and row["lease_token"] == leased["lease_token"]


def test_resolution_is_idempotent_and_conflicting_request_is_refused(service):
    backend, worker, job = pre_call_crash(service)
    backend.external_id = None
    cycle(service, worker)
    cycle(service, worker)
    args = {"decision": "ACKNOWLEDGE_UNRESOLVED", "reason": "checked twice", "subject": "op"}
    service.resolve_submission("team-a", job["job_id"], **args)
    service.resolve_submission("team-a", job["job_id"], **args)
    with pytest.raises(Conflict):
        service.resolve_submission("team-a", job["job_id"], **{**args, "subject": "other"})


def test_lifecycle_and_resolution_api_enforce_scope(service):
    tokens = {
        "user": Principal("team-a"),
        "op": Principal("team-a", operator=True, subject="ops-1"),
        "anon-op": Principal("team-a", operator=True),
        "foreign-op": Principal("team-b", operator=True, subject="ops-2"),
    }
    client = TestClient(
        create_app(service, {hashlib.sha256(t.encode()).hexdigest(): p for t, p in tokens.items()})
    )
    backend, worker, job = pre_call_crash(service)
    backend.external_id = None
    cycle(service, worker)
    cycle(service, worker)
    url = PREFIX + "/jobs/" + job["job_id"]
    payload = {"decision": "ACKNOWLEDGE_UNRESOLVED", "reason": "verified no native job"}

    def call(token, method="post"):
        headers = {"Authorization": "Bearer " + token}
        if method == "get":
            return client.get(url + "/lifecycle", headers=headers)
        return client.post(url + "/submission-resolution", headers=headers, json=payload)

    view = call("user", "get").json()
    assert view["resolution"]["eligible"] is True
    assert view["wait"]["category"] == "NO_NATIVE_IDENTITY"
    assert call("foreign-op", "get").status_code == 404
    assert call("user").status_code == 403
    assert call("anon-op").status_code == 403
    assert call("foreign-op").status_code == 404
    response = call("op")
    assert response.status_code == 200
    assert body(service, job["job_id"])["body"]["submission_resolution"]["requested_by"] == "ops-1"


def test_native_rejection_is_terminal_with_proven_zero_allocation(service):
    backend = NativeDouble()

    def reject(_job):
        backend.submissions += 1
        raise SubmissionRejected("SLURM_QOS_POLICY:QOSMaxCpuPerJobLimit")

    backend.submit = reject
    worker = Worker(service, {("team-a", "lab"): backend})
    job = submit(service)
    worker.submit_one()
    current = body(service, job["job_id"])
    assert current["state"] == "FAILED"
    assert current["body"]["error"] == "NATIVE_REJECTED:SLURM_QOS_POLICY:QOSMaxCpuPerJobLimit"
    assert classify_wait(current)["category"] == "NATIVE_REJECTED"
    [record] = ledger(service)
    assert record["allocated_device_seconds"] == 0
    assert record["body"]["native_rejected"] is True and record["body"]["uncertainty"] == []
    expire_retry(service)
    assert worker.submit_one() is False and backend.reconciles == 0


SLURM_QOS = (
    "sbatch: error: QOSMaxCpuPerJobLimit\n"
    "sbatch: error: Batch job submission failed: Job violates accounting/QOS policy "
    "(job submit limit, user's size and/or time limits)"
)


@pytest.mark.parametrize(
    "stderr, code",
    [
        (SLURM_QOS, "SLURM_QOS_POLICY:QOSMaxCpuPerJobLimit"),
        (
            "sbatch: error: Batch job submission failed: Invalid qos specification",
            "SLURM_INVALID_QOS",
        ),
        (
            "sbatch: error: Batch job submission failed: Socket timed out on send/recv operation",
            None,
        ),
        ("ssh: connect to host lab port 22: Connection timed out", None),
        (
            "Error from server (Forbidden): exceeded quota: lab, requested: nvidia.com/gpu=2",
            "KUBERNETES_FORBIDDEN",
        ),
        ('Error from server (AlreadyExists): jobs.batch "a-1" already exists', None),
        ("Error from server (Timeout): the server was unable to return a response", None),
        ("RA_NATIVE_REJECTED:SLURM_INVALID_QOS\n", None),
        ("banner sbatch: error: Batch job submission failed: Invalid qos specification", None),
        ("sbatch: error: Batch job submission failed: Invalid qos specification extra", None),
        (
            SLURM_QOS + "\nsbatch: error: Batch job submission failed: Invalid qos specification",
            None,
        ),
        ("warning\nError from server (Forbidden): denied", None),
        ("Error from server (InternalError): Internal error occurred", None),
        ("", None),
    ],
)
def test_only_allow_listed_native_refusals_are_rejections(stderr, code):
    assert native_rejection(stderr) == code


def test_run_attaches_rejection_and_adapters_split_it_from_uncertainty(monkeypatch, service):
    from resource_advisor import backends

    def fake(stderr):
        return lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", stderr)

    monkeypatch.setattr(backends.subprocess, "run", fake(SLURM_QOS))
    with pytest.raises(BackendError) as caught:
        backends.run(["sbatch", "--parsable"])
    assert caught.value.rejection == "SLURM_QOS_POLICY:QOSMaxCpuPerJobLimit"
    assert "QOS" not in str(caught.value)  # Raw stderr never reaches the message.

    def execute_with(rejection):
        def execute(*_a, **_k):
            error = BackendError("exited 1")
            error.rejection = rejection
            raise error

        return execute

    from test_backends import native_runtime, row

    job = row(service)
    job["body"]["capability"]["resource_key"] = "gpu:orin_nano"
    for rejection, expected in [("SLURM_INVALID_QOS", SubmissionRejected), (None, BackendError)]:
        slurm = SlurmBackend(
            partition="gpu",
            account="team-a",
            qos="lab",
            output_dir="/tmp/ra",
            native_runtimes=native_runtime(job),
            execute=execute_with(rejection),
        )
        with pytest.raises(expected) as caught:
            slurm.submit(job)
        assert (type(caught.value).__name__ == "SubmissionRejected") == bool(rejection)
    kubernetes = KubernetesBackend(
        namespace="research-a",
        local_queue="batch",
        node_selector={"pool": "lab"},
        execute=execute_with("KUBERNETES_FORBIDDEN"),
    )
    kubernetes.manifest = lambda _job: {}
    with pytest.raises(SubmissionRejected):
        kubernetes.submit(job)


def test_slurm_reconcile_multiple_matches_is_ambiguity(service):
    from test_backends import row

    job = row(service)
    name = job["body"]["attempt_id"]
    lines = f"11|{name}|team-a|gpu\n12|{name}|team-a|gpu\n"
    slurm = SlurmBackend(
        partition="gpu",
        account="team-a",
        qos="lab",
        output_dir="/tmp/ra",
        execute=lambda *a, **k: lines,
    )
    with pytest.raises(SubmissionAmbiguous):
        slurm.reconcile(job)


@pytest.mark.parametrize("foreign_annotations", [True, False])
def test_recreated_same_name_kubernetes_job_is_never_adopted(service, foreign_annotations):
    from test_backends import row

    job = row(service)
    owner = {
        "resource-advisor/job-id": job["id"],
        "resource-advisor/context-signature": job["body"]["context_signature"],
    }
    native = {"metadata": {"name": job["body"]["attempt_id"], "uid": "u1", "annotations": owner}}

    def execute(args, **_kwargs):
        if "create" in args:
            return json.dumps(native)
        if "pods" in args:
            return json.dumps({"items": []})
        return json.dumps(native)

    backend = KubernetesBackend(
        namespace="research-a", local_queue="batch", node_selector={"pool": "lab"}, execute=execute
    )
    worker = Worker(service, {("team-a", job["body"]["backend_cluster_id"]): backend})
    worker.submit_one()
    assert body(service, job["id"])["body"]["backend_uid"] == "u1"
    # Retention disabled: the original Job is deleted and another with the same
    # name appears, which has failed.
    native = {
        "metadata": {
            "name": job["body"]["attempt_id"],
            "uid": "u2",
            "annotations": {"resource-advisor/job-id": "j-foreign"}
            if foreign_annotations
            else owner,
        },
        "status": {"conditions": [{"type": "Failed", "status": "True", "reason": "Foreign"}]},
    }
    worker.reconcile_all()
    current = service.get_job("team-a", job["id"])
    assert current["state"] == "QUEUED"
    assert current["last_observation_error"] == "BackendError"
    assert ledger(service) == []
    with pytest.raises(SubmissionAmbiguous):
        backend.reconcile(body(service, job["id"]))


@pytest.mark.parametrize(
    "records,expected",
    [
        (["42|{n}|team-a|gpu|2026-10-29T01:00:00", "42|{n}|team-a|gpu|2026-10-29T01:00:00"], "42"),
        (["42|{n}|team-a|gpu|2026-10-29T01:00:00", "42|{n}|team-a|gpu|2026-10-29T02:00:00"], None),
    ],
)
def test_slurm_reconcile_keeps_reused_id_history_distinct(service, records, expected):
    from test_backends import row

    job = row(service)
    name = job["body"]["attempt_id"]
    calls = []

    def execute(argv, **_kwargs):
        calls.append(argv)
        return "" if argv[0] == "squeue" else "\n".join(r.format(n=name) for r in records)

    slurm = SlurmBackend(
        partition="gpu", account="team-a", qos="lab", output_dir="/tmp/ra", execute=execute
    )
    if expected is None:
        with pytest.raises(SubmissionAmbiguous):
            slurm.reconcile(job)
    else:
        assert slurm.reconcile(job) == expected
    sacct = next(argv for argv in calls if "sacct" in argv)
    assert "--duplicates" in sacct and sacct[-1].endswith(",Submit")


def test_gateway_accepts_only_exact_history_reconcile_forms():
    spec = importlib.util.spec_from_file_location(
        "slurm_executor", Path(__file__).parents[1] / "examples/slurm_executor.py"
    )
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    config = {"mode": "controller", "user": "executor", "account": "lab", "partition": "compute"}
    base = "env TZ=UTC SLURM_TIME_FORMAT=%Y-%m-%dT%H:%M:%S%z sacct --noheader --parsable2"
    tail = "--name a-1 --accounts lab --partition compute --starttime 2026-10-29T00:00:00"
    fields = "--format=JobIDRaw,JobName%100,Account%100,Partition%100"
    for accepted in [f"{base} --duplicates {tail} {fields},Submit", f"{base} {tail} {fields}"]:
        assert gateway.plan(config, accepted)[0][-2:] == ["--user", "executor"]
    for refused in [
        f"{base} --duplicates {tail} {fields}",
        f"{base} {tail} {fields},Submit",
        f"{base} --duplicates {tail.replace('lab', 'other')} {fields},Submit",
    ]:
        with pytest.raises(ValueError):
            gateway.plan(config, refused)


def test_gateway_forwards_only_slurm_rejection_lines(monkeypatch, capsys, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "gateway", Path(__file__).parents[1] / "examples/slurm_executor.py"
    )
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setattr(gateway, "plan", lambda *a: (["/usr/bin/sbatch", "--parsable"], "x"))
    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", SLURM_QOS + "\nsecret /path"),
    )
    monkeypatch.setattr("sys.argv", ["gateway", "--config", str(config)])
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "squeue")
    assert gateway.main() == 1
    err = capsys.readouterr().err
    assert native_rejection(err) == "SLURM_QOS_POLICY:QOSMaxCpuPerJobLimit"
    assert "secret" not in err


def test_restarted_worker_and_repeated_reconcile_add_no_usage(service):
    backend = NativeDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    job = submit(service)
    worker.submit_one()
    started = now().replace(microsecond=0)
    backend.observation = Observation(
        State.COLLECTING,
        (started - timedelta(seconds=10)).isoformat(),
        started.isoformat(),
        allocation={"source": "double", "accelerator_count": 1, "cpu": 1, "memory_mib": 512},
    )
    worker.reconcile_all()
    restarted = Worker(service, {("team-a", "lab"): backend})
    for _ in range(3):
        restarted.reconcile_all()
        expire_retry(service)
        restarted.submit_one()
        restarted.cancel_one()
    assert service.store.backfill_usage() == 0
    current = body(service, job["job_id"])
    assert current["state"] == "SUCCEEDED"
    [record] = ledger(service)
    assert record["allocated_device_seconds"] == 10
    assert backend.submissions == 1


@pytest.mark.parametrize(
    "backend, state, reason, admission, category",
    [
        ("slurm", "QUEUED", "AssocGrpCpuLimit", None, "QUOTA_OR_LIMIT_PENDING"),
        ("slurm", "QUEUED", "Resources", None, "CAPACITY_PENDING"),
        ("slurm", "QUEUED", "Priority", None, "PRIORITY_PENDING"),
        ("slurm", "QUEUED", "SomethingNew", None, "UNCLASSIFIED"),
        ("slurm", "QUEUED", None, None, "UNKNOWN"),
        (
            "kubernetes",
            "QUEUED",
            "AdmissionPending",
            {
                "source": "kueue:Workload",
                "conditions": [
                    {
                        "type": "QuotaReserved",
                        "status": "False",
                        "message": "couldn't assign flavors to pod set main: insufficient "
                        "unused quota for nvidia.com/gpu in flavor ra-f0-gpu, 1 more needed",
                    }
                ],
            },
            "QUOTA_OR_LIMIT_PENDING",
        ),
        (
            "kubernetes",
            "QUEUED",
            "Unschedulable",
            {"conditions": [{"type": "Admitted", "status": "True"}]},
            "CAPACITY_PENDING",
        ),
        (
            "kubernetes",
            "QUEUED",
            "AdmissionPending",
            {"error": "UNAVAILABLE"},
            "ADMISSION_UNOBSERVED",
        ),
        ("kubernetes", "RUNNING", None, None, "NOT_WAITING"),
    ],
)
def test_native_wait_is_classified_without_conflating_rejection(
    backend, state, reason, admission, category
):
    row = {
        "state": state,
        "body": {
            "candidate": {"backend": backend},
            "external_id": "1",
            "scheduler_reason": reason,
            "admission": admission,
        },
    }
    assert classify_wait(row) == {"backend": backend, "category": category, "native_reason": reason}


def usage_record(project="team-a", start=None, end=None, *, count=1, cpu=2, **extra):
    body = {
        "schema_version": "v2",
        "backend_cluster_id": extra.pop("cluster", "lab"),
        "accelerator_model": extra.pop("model", "fixture-gpu"),
        "resource_key": extra.pop("resource_key", "nvidia.com/gpu"),
        "outcome": extra.pop("outcome", "SUCCEEDED"),
        "started_at": start,
        "backend_finished_at": end,
        "observed_allocation": {"accelerator_count": count, "cpu": cpu},
        "uncertainty": [],
        **extra.pop("body", {}),
    }
    return {
        "project": project,
        "backend": "kubernetes",
        "device_class": extra.pop("device_class", "gpu"),
        "allocation_mode": extra.pop("mode", "physical_device"),
        "body": body,
    }


def test_monthly_report_clips_reservations_at_explicit_utc_boundaries():
    start, end = month_period("2026-10")
    assert (start.isoformat(), end.isoformat()) == (
        "2026-10-01T00:00:00+00:00",
        "2026-11-01T00:00:00+00:00",
    )
    assert month_period("2026-12")[1].isoformat() == "2027-01-01T00:00:00+00:00"
    records = [
        # Crosses the start boundary in another offset: 30 minutes inside October UTC.
        usage_record(start="2026-10-01T08:00:00+09:00", end="2026-10-01T09:30:00+09:00"),
        # Crosses the end boundary: 60 seconds inside.
        usage_record(start="2026-10-31T23:59:00+00:00", end="2026-11-01T00:05:00+00:00"),
        # Entirely in September: excluded.
        usage_record(start="2026-09-10T00:00:00+00:00", end="2026-09-10T01:00:00+00:00"),
        # Ends exactly at the start boundary: excluded, half-open interval.
        usage_record(start="2026-09-30T23:00:00+00:00", end="2026-10-01T00:00:00+00:00"),
        # Unknown interval attributed by terminal observation: unknown, not zero.
        usage_record(body={"terminal_observed_at": "2026-10-15T00:00:00+00:00"}),
        # Unknown interval without any timestamp: reported globally.
        usage_record(),
        # Legacy record: never silently included.
        {**usage_record(), "body": {"schema_version": "v1"}},
        # Known interval, unknown accelerator count.
        usage_record(
            start="2026-10-02T00:00:00+00:00", end="2026-10-02T00:00:10+00:00", count=None
        ),
    ]
    report = period_report(records, start, end)
    [group] = report["groups"]
    assert group["attempts"] == 4
    assert group["clipped_attempts"] == 2
    assert group["known_allocated_device_seconds"] == 30 * 60 + 60
    assert group["known_allocated_cpu_seconds"] == 2 * (30 * 60 + 60 + 10)
    assert group["unknown_allocation_attempts"] == 2
    assert report["unattributable_attempts"] == 2
    assert report["period"]["boundary"] == "[start, end) UTC"
    with pytest.raises(ValueError):
        period_report(records, "2026-10-01T00:00:00", "2026-11-01T00:00:00")
    with pytest.raises(ValueError):
        period_report(records, end, start)


def test_unknown_interval_stays_in_every_period_it_could_overlap():
    late = usage_record(
        start="2026-10-31T23:00:00+00:00",
        end=None,
        body={"terminal_observed_at": "2026-11-01T00:10:00+00:00"},
    )
    submitted_only = usage_record(
        body={
            "submitted_at": "2026-11-02T00:00:00+00:00",
            "terminal_observed_at": "2026-11-02T00:05:00+00:00",
        }
    )
    october = period_report([late, submitted_only], *month_period("2026-10"))
    november = period_report([late, submitted_only], *month_period("2026-11"))
    september = period_report([late, submitted_only], *month_period("2026-09"))
    [group] = october["groups"]
    assert group["attempts"] == group["unknown_allocation_attempts"] == 1
    assert group["known_allocated_device_seconds"] == 0
    assert november["groups"][0]["unknown_allocation_attempts"] == 2
    assert september["groups"] == [] and september["unattributable_attempts"] == 0


def test_report_keeps_units_clusters_and_rejections_separate():
    window = ("2026-10-02T00:00:00+00:00", "2026-10-02T00:01:40+00:00")
    records = [
        usage_record("team-a", *window),
        usage_record("team-a", *window, cluster="other-lab"),
        usage_record("team-a", *window, mode="virtual_slot", resource_key="nvidia.com/gpu.shared"),
        usage_record("team-a", *window, resource_key="nvidia.com/mig-1g.10gb"),
        usage_record(
            "team-a", *window, device_class="npu", model="hailo8", resource_key="hailo.ai/h8"
        ),
        usage_record(
            body={
                "never_submitted": True,
                "native_rejected": True,
                "terminal_observed_at": window[0],
            }
        ),
    ]
    report = period_report(records, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    groups = report["groups"]
    assert len(groups) == 5
    assert all(g["known_allocated_device_seconds"] == 100 for g in groups)
    physical = next(
        g
        for g in groups
        if g["backend_cluster_id"] == "lab" and g["resource_key"] == "nvidia.com/gpu"
    )
    assert physical["attempts"] == 2 and physical["no_native_job_attempts"] == 1
    assert physical["known_allocated_device_seconds"] == 100
    assert physical["unknown_allocation_attempts"] == 0


def test_usage_report_api_scopes_project_and_institution(service):
    tokens = {
        "user": Principal("team-a"),
        "inst": Principal(
            "team-a", operator=True, subject="ops", institution_projects=("team-a", "team-b")
        ),
    }
    client = TestClient(
        create_app(service, {hashlib.sha256(t.encode()).hexdigest(): p for t, p in tokens.items()})
    )
    with service.store.transaction() as conn:
        for project in ("team-a", "team-b", "team-c"):
            rec = usage_record(project, "2026-10-02T00:00:00+00:00", "2026-10-02T00:00:10+00:00")
            conn.execute(
                usage.insert().values(
                    attempt_id="a-" + project,
                    project=project,
                    backend=rec["backend"],
                    device_class=rec["device_class"],
                    allocation_mode=rec["allocation_mode"],
                    body=rec["body"],
                )
            )

    def get(token, **params):
        return client.get(
            PREFIX + "/usage/report", params=params, headers={"Authorization": "Bearer " + token}
        )

    own = get("user", month="2026-10").json()
    assert own["projects"] == ["team-a"] and own["groups"][0]["attempts"] == 1
    assert get("user", month="2026-10", scope="institution").status_code == 403
    inst = get("inst", month="2026-10", scope="institution").json()
    assert inst["groups"][0]["projects"] == {"team-a": 1, "team-b": 1}
    assert get("user").status_code == 422
    assert get("user", month="2026-13").status_code == 422
    assert get("user", start="2026-10-01T00:00:00", end="2026-11-01T00:00:00").status_code == 422
    ranged = get("user", start="2026-10-02T00:00:05+00:00", end="2026-10-03T00:00:00+00:00").json()
    assert ranged["groups"][0]["known_allocated_device_seconds"] == 5


def test_institution_scope_is_operator_only_and_includes_own_project():
    digest = "a" * 64
    ok = principals(
        {
            digest: {
                "project": "team-a",
                "operator": True,
                "institution_projects": ["team-b", "team-a"],
            }
        }
    )
    assert ok[digest].institution_projects == ("team-a", "team-b")
    for identity in [
        {"project": "team-a", "institution_projects": ["team-a"]},
        {"project": "team-a", "operator": True, "institution_projects": ["team-b"]},
    ]:
        with pytest.raises(ConfigurationError):
            principals({digest: identity})


@pytest.mark.parametrize(
    "error, title",
    [
        ("NATIVE_REJECTED:SLURM_INVALID_QOS", "스케줄러 정책이 제출 거부"),
    ],
)
def test_console_attention_distinguishes_rejection_from_unresolved(error, title):
    from resource_advisor.operations import job_attention

    assert job_attention({"state": "FAILED", "error": error})["title"] == title
    acknowledged = {
        "state": "SUBMISSION_UNKNOWN",
        "submission_resolution": {"status": "ACKNOWLEDGED"},
    }
    assert job_attention(acknowledged)["title"] == "제출 결과 미확정(운영자 확인됨)"


def trial_module():
    spec = importlib.util.spec_from_file_location(
        "trial", Path(__file__).parents[1] / "examples/integrated_operations_trial.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRIAL_PLAN = {
    "trial_ref": "io-1",
    "project": "team-a",
    "workload_ref": "w",
    "candidate_ref": "c",
    "keys": [f"io-1-{i}" for i in range(10)],
    "job_seconds": 120,
    "window_seconds": 1800,
    "accelerators_per_job": 1,
}


def test_trial_plan_enforces_frozen_budget(tmp_path):
    trial = trial_module()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(TRIAL_PLAN))
    assert trial.load_plan(path)[1] == 1200
    for change in [
        {"keys": [f"io-1-{i}" for i in range(31)], "accelerators_per_job": 0},
        {"job_seconds": 121},
        {"window_seconds": 1801},
        {"accelerators_per_job": 2},
        {"keys": ["other-1"]},
        {"previous_native_jobs": 0},
        {"unexpected": True},
    ]:
        path.write_text(json.dumps({**TRIAL_PLAN, **change}))
        with pytest.raises(ValueError):
            trial.load_plan(path)


class TrialApi:
    """Compute API double: every key creates one job whose view reports limits."""

    def __init__(self, max_run_seconds=120, accelerators=1):
        self.jobs, self.limits = {}, (max_run_seconds, accelerators)

    def call(self, method, path, body=None, headers=None):
        if method == "POST":
            key = headers["Idempotency-Key"]
            job = self.jobs.setdefault(key, {"job_id": "j-" + key, "attempt_id": "a-" + key})
            return 200, job
        seconds, count = self.limits
        return 200, {
            "execution_limits": {"max_run_seconds": seconds},
            "requested_resources": {"accelerator_count": count},
        }


def test_trial_budget_is_cumulative_and_needs_closure(tmp_path):
    trial = trial_module()
    budget = tmp_path / "budget.json"
    second = {**TRIAL_PLAN, "trial_ref": "io-2", "keys": [f"io-2-{i}" for i in range(10)]}
    assert trial.submit(TrialApi(), TRIAL_PLAN, tmp_path / "c1", budget) is None
    with pytest.raises(ValueError, match="not closed"):
        trial.submit(TrialApi(), second, tmp_path / "c2", budget)
    state = json.loads(budget.read_text())
    state["cohorts"][0]["closed"] = True
    budget.write_text(json.dumps(state))
    # Two 10-job, 120 s, one-accelerator cohorts would reserve 2400 > 1800.
    with pytest.raises(ValueError, match="accelerator-slot"):
        trial.submit(TrialApi(), second, tmp_path / "c2", budget)
    state = json.loads(budget.read_text())
    assert len(state["cohorts"]) == 1
    # Observation keeps the persisted deadline: a re-run after the window ends
    # takes one sample and stops instead of restarting the clock.
    state["window_started_at"] = state["cohorts"][0]["submitted_at"] = "2026-10-29T00:00:00+00:00"
    budget.write_text(json.dumps(state))
    trial.observe(TrialApi(), TRIAL_PLAN, tmp_path / "c1", budget)
    observed = json.loads((tmp_path / "c1" / "observe.json").read_text())
    assert not observed["settled"] and len(observed["samples"]) == 1


def test_trial_submit_stops_when_actual_limits_exceed_plan(tmp_path):
    trial = trial_module()
    budget = tmp_path / "budget.json"
    stop = trial.submit(TrialApi(max_run_seconds=600), TRIAL_PLAN, tmp_path / "c1", budget)
    assert stop == "LIMITS_EXCEED_PLAN"
    [cohort] = json.loads(budget.read_text())["cohorts"]
    assert cohort["native_jobs"] == 1 and cohort["slot_seconds"] == 600
    assert len(json.loads((tmp_path / "c1" / "submit.json").read_text())) == 1


def audit_inputs():
    jobs = [
        {
            "key": f"io-1-{i}",
            "job_id": f"j{i}",
            "attempt_id": f"a{i}",
            "status": [200, 200],
            "replay_same_identity": True,
        }
        for i in range(3)
    ]
    views = {
        "j0": {"state": "SUCCEEDED", "native_identity": {"external_id": "10"}},
        "j1": {
            "state": "FAILED",
            "native_identity": {"external_id": None},
            "native_rejection": {"code": "QOS"},
        },
        "j2": {"state": "SUBMISSION_UNKNOWN", "native_identity": {"external_id": None}},
    }
    measured = {
        "started_at": "2026-10-29T00:00:00+00:00",
        "backend_finished_at": "2026-10-29T00:00:30+00:00",
        "observed_allocation": {"accelerator_count": 1},
        "outcome": "SUCCEEDED",
    }
    ledger = [
        {"attempt_id": "a0", "allocated_device_seconds": 30.0, "body": measured},
        {"attempt_id": "a1", "allocated_device_seconds": None, "body": {"outcome": "FAILED"}},
        {"attempt_id": "foreign", "allocated_device_seconds": 99.0, "body": measured},
    ]
    native = {"a0": [{"native_id": "10", "terminal": True}], "a1": [], "a2": []}
    keys = [j["key"] for j in jobs]
    return jobs, {"settled": True, "final": views}, ledger, native, keys


def test_trial_audit_keeps_unknown_unknown_and_never_passes_unresolved():
    trial = trial_module()
    jobs, observed, ledger, native, keys = audit_inputs()
    result = trial.audit(jobs, observed, ledger, native, keys)
    assert result["contained"] and not result["passed"]
    assert result["unknown_usage_attempts"] == 1 and result["unresolved_attempts"] == 1
    assert result["known_device_seconds"] == 30.0
    assert {"attempt_id": "a2", "issue": "UNRESOLVED_SUBMISSION"} in result["findings"]
    del observed["final"]["j2"], native["a2"]
    jobs, keys = jobs[:2], keys[:2]
    assert trial.audit(jobs, observed, ledger, native, keys)["passed"]
    doubled = trial.audit(jobs, observed, ledger + [ledger[0]], native, keys)
    assert {"attempt_id": "a0", "issue": "TERMINAL_WITHOUT_ONE_LEDGER_ROW"} in doubled["findings"]
    wrong = trial.audit(
        jobs, observed, [{**ledger[0], "allocated_device_seconds": 60.0}, ledger[1]], native, keys
    )
    assert {"attempt_id": "a0", "issue": "USAGE_RECALCULATION_DIFFERS"} in wrong["findings"]
    twice = trial.audit(jobs, observed, ledger, {**native, "a0": native["a0"] * 2}, keys)
    assert {"attempt_id": "a0", "issue": "NATIVE_COUNT_DISAGREES"} in twice["findings"]


@pytest.mark.parametrize(
    "change",
    [
        "forbidden_submissions",
        "missing_view",
        "running_without_ledger",
        "unsettled",
        "missing_receipt",
        "active_native",
    ],
)
def test_trial_audit_refuses_unfinished_or_unidentified_trials(change):
    trial = trial_module()
    jobs, observed, ledger, native, keys = audit_inputs()
    if change == "forbidden_submissions":
        for job in jobs:
            job.update(status=[403, 403], job_id=None, attempt_id=None)
    elif change == "missing_view":
        del observed["final"]["j0"]
    elif change == "running_without_ledger":
        observed["final"]["j0"]["state"] = "RUNNING"
        ledger = ledger[1:]
    elif change == "unsettled":
        observed["settled"] = False
    elif change == "missing_receipt":
        del native["a0"]
    else:
        native["a2"] = [{"native_id": "11", "terminal": False}]
    result = trial.audit(jobs, observed, ledger, native, keys)
    assert not result["passed"]
    if change != "forbidden_submissions":
        assert not result["contained"]


def test_period_report_lists_unresolved_attempts_as_not_finalized(service):
    backend, worker, job = pre_call_crash(service)
    backend.external_id = None
    cycle(service, worker)
    token = "user"
    client = TestClient(
        create_app(service, {hashlib.sha256(token.encode()).hexdigest(): Principal("team-a")})
    )
    month = job["created_at"][:7]
    report = client.get(
        PREFIX + "/usage/report",
        params={"month": month},
        headers={"Authorization": "Bearer " + token},
    ).json()
    assert report["groups"] == []
    assert report["not_finalized"] == [
        {
            "backend_cluster_id": "lab",
            "backend": "kubernetes",
            "device_class": "gpu",
            "allocation_mode": "physical_device",
            "state": "SUBMISSION_UNKNOWN",
            "native_identity_unknown": True,
            "attempts": 1,
        }
    ]
    earlier = period_report(
        [], "2000-01-01T00:00:00Z", "2000-02-01T00:00:00Z", [body(service, job["job_id"])]
    )
    assert earlier["not_finalized"] == []
