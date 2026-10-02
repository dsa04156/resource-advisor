"""Adversarial worker interleavings; scheduler doubles are not hardware evidence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, update
from test_worker import SchedulerDouble

from resource_advisor.backends import Observation
from resource_advisor.contracts import ExecutionResult, JobRequest, State, now
from resource_advisor.store import Conflict, entities, outbox, usage
from resource_advisor.worker import Worker


def test_delayed_submit_response_preserves_newer_worker_state(service):
    backend = SchedulerDouble()
    worker_b = Worker(service, {("team-a", "lab"): backend})
    submit = backend.submit

    def delayed(row):
        external = submit(row)
        # A is still alive, but B legitimately reclaims its expired lease.
        with service.store.transaction() as conn:
            conn.execute(
                update(outbox).values(lease_until=(now() - timedelta(seconds=1)).isoformat())
            )
        assert worker_b.submit_one()
        backend.observation = Observation(State.RUNNING)
        worker_b.reconcile_all()
        assert service.get_job("team-a", row["id"])["state"] == "RUNNING"
        return external

    backend.submit = delayed
    worker_a = Worker(service, {("team-a", "lab"): backend})
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "late"
    )
    worker_a.submit_one()
    assert backend.submissions == 1
    assert service.get_job("team-a", job["job_id"])["state"] == "RUNNING"


def test_delayed_submit_response_does_not_reopen_completed_job(service):
    backend = SchedulerDouble()
    worker_b = Worker(service, {("team-a", "lab"): backend})
    submit = backend.submit

    def delayed(row):
        external = submit(row)
        with service.store.transaction() as conn:
            conn.execute(
                update(outbox).values(lease_until=(now() - timedelta(seconds=1)).isoformat())
            )
        assert worker_b.submit_one()
        backend.observation = Observation(State.COLLECTING)
        worker_b.reconcile_all()
        assert service.get_job("team-a", row["id"])["state"] == "SUCCEEDED"
        return external

    backend.submit = delayed
    worker_a = Worker(service, {("team-a", "lab"): backend})
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "late"
    )
    worker_a.submit_one()
    assert backend.submissions == 1
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
    with service.store.transaction() as conn:
        assert len(conn.execute(select(usage)).all()) == 1


def test_delayed_submit_response_preserves_concurrent_cancellation(service):
    backend = SchedulerDouble()
    worker_b = Worker(service, {("team-a", "lab"): backend})
    submit = backend.submit

    def delayed(row):
        external = submit(row)
        with service.store.transaction() as conn:
            conn.execute(
                update(outbox).values(lease_until=(now() - timedelta(seconds=1)).isoformat())
            )
        worker_b.submit_one()
        service.cancel("team-a", row["id"])
        backend.observation = Observation(State.CANCELED)
        worker_b.cancel_one()
        assert service.get_job("team-a", row["id"])["state"] == "CANCELED"
        return external

    backend.submit = delayed
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "cancel-race"
    )
    Worker(service, {("team-a", "lab"): backend}).submit_one()
    assert backend.submissions == 1
    assert service.get_job("team-a", job["job_id"])["state"] == "CANCELED"


def test_concurrent_immutable_insert_preserves_first_content(service, monkeypatch):
    # Simulate an initially stale absence read without threads. The unique-key
    # winner must never be accepted for a different body or owning project.
    with service.store.transaction() as conn:
        service.store.put(conn, "fixture", "winner", "team-a", {"value": 1})
    get = service.store.get

    for project, body in [("team-a", {"value": 2}), ("team-b", {"value": 1})]:
        reads = []

        def stale_absence(conn, kind, ref, reads=reads):
            reads.append(True)
            return None if len(reads) == 1 else get(conn, kind, ref)

        monkeypatch.setattr(service.store, "get", stale_absence)
        with pytest.raises(Conflict, match="immutable"):
            with service.store.transaction() as conn:
                service.store.put(conn, "fixture", "winner", project, body)
    monkeypatch.setattr(service.store, "get", get)
    with service.store.transaction() as conn:
        winner = get(conn, "fixture", "winner")
        assert winner["body"] == {"value": 1} and winner["project"] == "team-a"


def test_status_observed_before_another_worker_transition_cannot_regress(service):
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "race"
    )
    worker.submit_one()
    stale = worker._row(job["job_id"])
    with service.store.transaction() as conn:
        service.store.change_job(conn, stale, State.COLLECTING, stale["body"])
    from resource_advisor.store import Conflict

    try:
        worker._reconcile_observation(stale, backend, Observation(State.RUNNING))
    except Conflict:
        pass  # The next cycle must obtain a fresh scheduler observation.
    assert service.get_job("team-a", job["job_id"])["state"] == "COLLECTING"


def test_postgres_concurrent_collectors_commit_one_result_and_ledger(service, monkeypatch):
    if service.store.engine.dialect.name != "postgresql":
        pytest.skip("Independent concurrent transactions require the PostgreSQL test job")
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "collect"
    )
    worker.submit_one()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.COLLECTING, row["body"])
    envelope = backend.result(worker._row(job["job_id"]))
    result = ExecutionResult.model_validate(envelope["result"])
    barrier = Barrier(2)
    get = service.store.get

    def concurrent_read(conn, kind, ref):
        value = get(conn, kind, ref)
        if kind == "result" and value is None:
            barrier.wait(timeout=10)
        return value

    monkeypatch.setattr(service.store, "get", concurrent_read)

    def collect():
        try:
            service.ingest("team-a", result, envelope["digest"])
            return "committed"
        except Conflict:
            return "retry"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: collect(), range(2)))
    monkeypatch.setattr(service.store, "get", get)
    assert outcomes.count("committed") >= 1
    service.ingest("team-a", result, envelope["digest"])
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
    with service.store.transaction() as conn:
        assert len(conn.execute(select(usage)).all()) == 1
        for kind in ("result", "profile"):
            assert len(conn.execute(select(entities).where(entities.c.kind == kind)).all()) == 1
        assert sorted(conn.execute(select(outbox.c.kind)).scalars()) == [
            "artifact",
            "mlflow",
            "submit",
        ]


def test_postgres_claim_and_stale_finish_are_fenced(service):
    if service.store.engine.dialect.name != "postgresql":
        pytest.skip("Independent concurrent transactions require the PostgreSQL test job")
    service.submit("team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "claims")
    barrier = Barrier(8)

    def claim(_):
        barrier.wait(timeout=10)
        return service.store.claim("submit")

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = [e for e in pool.map(claim, range(8)) if e]
    assert len(claimed) == 1
    first = claimed[0]
    with service.store.transaction() as conn:
        conn.execute(
            update(outbox)
            .where(outbox.c.id == first["id"])
            .values(lease_until=(now() - timedelta(seconds=1)).isoformat())
        )
    second = service.store.claim("submit")
    assert second["tries"] == 2 and second["lease_token"] != first["lease_token"]
    service.store.finish(first)
    with service.store.transaction() as conn:
        current = conn.execute(select(outbox)).mappings().one()
        assert current["status"] == "LEASED" and current["lease_token"] == second["lease_token"]
    service.store.finish(second)
