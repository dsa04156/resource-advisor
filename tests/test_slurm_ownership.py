"""Scheduler-output doubles: these tests are not live Slurm execution evidence."""

import pytest
from sqlalchemy import select

from resource_advisor.backends import BackendError, SlurmBackend
from resource_advisor.contracts import JobRequest, State
from resource_advisor.store import usage
from resource_advisor.worker import Worker


@pytest.fixture
def job(service):
    submitted = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "ownership"
    )
    with service.store.transaction() as conn:
        saved = dict(service.store.job(conn, submitted["job_id"]))
    saved["body"]["external_id"] = "42"
    saved["body"]["capability"]["resource_key"] = "gpu:test"
    return saved


def backend(execute):
    return SlurmBackend(
        partition="gpu", account="team-a", qos="normal", output_dir="/tmp/ra", execute=execute
    )


def queue_record(job, **overrides):
    fields = dict(
        id="42",
        attempt=job["body"]["attempt_id"],
        account="team-a",
        partition="gpu",
        state="RUNNING",
    )
    fields.update(overrides)
    return "|".join(fields.values()) + "\n"


def accounting_record(job, **overrides):
    fields = dict(
        id="42",
        attempt=job["body"]["attempt_id"],
        account="team-a",
        partition="gpu",
        state="COMPLETED",
        exit="0:0",
        start="2026-10-02T05:00:00+0000",
        end="2026-10-02T05:00:02+0000",
        tres="cpu=2,mem=2G,gres/gpu=1",
        submit="2026-10-02T04:59:59+0000",
    )
    fields.update(overrides)
    return "|".join(fields.values()) + "\n"


@pytest.mark.parametrize("source", ["queue", "accounting"])
@pytest.mark.parametrize("field", ["attempt", "account", "partition", "duplicate", "truncated"])
def test_status_refuses_unowned_or_ambiguous_records(job, source, field):
    record = queue_record if source == "queue" else accounting_record
    output = (
        record(job, **{field: "foreign"})
        if field not in {"duplicate", "truncated"}
        else record(job)
    )
    if field == "duplicate":
        output *= 2
    elif field == "truncated":
        output = "|".join(output.split("|")[:-1])

    # Accounting cases must first observe a successful empty live queue.
    def route(args, **kwargs):
        if source == "accounting" and args[0] == "squeue":
            return ""
        return output

    with pytest.raises(BackendError):
        backend(route).status(job)


@pytest.mark.parametrize("source", ["queue", "accounting"])
def test_status_accepts_exact_owner_and_keeps_accounting_boundaries(job, source):
    commands = []

    def execute(args, **kwargs):
        commands.append(args)
        if args[0] == "squeue":
            return queue_record(job) if source == "queue" else ""
        return accounting_record(job)

    observation = backend(execute).status(job)
    assert observation.state == ("RUNNING" if source == "queue" else "COLLECTING")
    if source == "accounting":
        assert observation.allocation["accelerator_count"] == 1
        assert observation.submitted_at == "2026-10-02T04:59:59+0000"
        assert "--accounts" in commands[-1]
        assert "--duplicates" in commands[-1]


@pytest.mark.parametrize("source", ["queue", "accounting"])
def test_transport_failure_is_unknown_not_absent_or_terminal(job, source):
    def execute(args, **kwargs):
        if source == "accounting" and args[0] == "squeue":
            return ""
        raise BackendError("transport unavailable")

    with pytest.raises(BackendError, match="transport unavailable"):
        backend(execute).status(job)


def test_cancel_has_scheduler_side_identity_filters(job):
    commands = []
    backend(lambda args, **kwargs: commands.append(args)).cancel(job)
    assert commands == [
        [
            "scancel",
            "--ctld",
            "--account",
            "team-a",
            "--partition",
            "gpu",
            "--name",
            job["body"]["attempt_id"],
            "42",
        ]
    ]


def test_failed_parent_preserves_contained_step_oom(job):
    # Mirrors the retained real limits-v2 shape; this test itself uses doubles.
    output = accounting_record(job, state="FAILED", exit="1:0") + accounting_record(
        job, id="42.0", attempt="python3", partition="", state="OUT_OF_MEMORY", exit="0:125"
    )
    observation = backend(lambda args, **kwargs: "" if args[0] == "squeue" else output).status(job)
    assert observation.state == State.FAILED
    assert observation.error == "OUT_OF_MEMORY"
    assert observation.allocation["accelerator_count"] == 1
    assert observation.started_at == "2026-10-02T05:00:00+0000"
    assert observation.finished_at == "2026-10-02T05:00:02+0000"


@pytest.mark.parametrize(
    "change",
    [
        {"id": "420.0"},
        {"id": "42.0.extra"},
        {"account": "other-team"},
        {"partition": "other-pool"},
        {"start": "2026-10-01T05:00:00+0000"},
        {"end": "2026-10-03T05:00:00+0000"},
        {"end": "2026-10-02T04:59:59+0000"},
        {"start": "Unknown"},
        {"end": "2026-10-02T05:00:02"},
    ],
)
def test_unrelated_or_unbounded_step_does_not_relabel_failure(job, change):
    fields = dict(id="42.0", attempt="python3", partition="", state="OUT_OF_MEMORY")
    fields.update(change)
    output = accounting_record(job, state="FAILED", exit="1:0") + accounting_record(job, **fields)
    observation = backend(lambda args, **kwargs: "" if args[0] == "squeue" else output).status(job)
    assert observation.error == "FAILED"


@pytest.mark.parametrize(
    "state, expected", [("COMPLETED", State.COLLECTING), ("CANCELLED", State.CANCELED)]
)
def test_step_oom_does_not_override_parent_success_or_cancel(job, state, expected):
    output = accounting_record(job, state=state) + accounting_record(
        job, id="42.0", attempt="python3", partition="", state="OUT_OF_MEMORY"
    )
    observation = backend(lambda args, **kwargs: "" if args[0] == "squeue" else output).status(job)
    assert observation.state == expected
    assert observation.error is None


@pytest.mark.parametrize("external_id", ["--user=someone", "42.batch", "42_1", "42,43", "", "-1"])
def test_invalid_cancel_id_never_reaches_scheduler(job, external_id):
    job["body"]["external_id"] = external_id
    commands = []
    with pytest.raises(BackendError, match="external ID"):
        backend(lambda args, **kwargs: commands.append(args)).cancel(job)
    assert commands == []


@pytest.mark.parametrize("fault", ["foreign", "unavailable"])
def test_worker_preserves_pending_cancel_without_foreign_ledger_or_mutation(service, job, fault):
    with service.store.transaction() as conn:
        current = service.store.job(conn, job["id"])
        service.store.change_job(conn, current, State.QUEUED, job["body"])
    service.cancel("team-a", job["id"])
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if fault == "unavailable":
            raise BackendError("unavailable")
        assert args[0] == "squeue"
        return queue_record(job, attempt="other-attempt")

    worker = Worker(service, {("team-a", "lab"): backend(execute)})
    worker.cancel_one()
    worker.reconcile_all()
    current = service.get_job("team-a", job["id"])
    assert current["state"] == "CANCEL_REQUESTED"
    assert current["last_observation_error"] == "BackendError"
    assert not current.get("cancel_acknowledged_at")
    assert all(args[0] == "squeue" for args in calls)
    with service.store.transaction() as conn:
        assert conn.execute(select(usage)).first() is None
