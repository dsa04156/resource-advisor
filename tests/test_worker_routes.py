"""Real SQL route/lease boundaries with explicit scheduler doubles, not GPU evidence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, update
from test_worker import SchedulerDouble

from resource_advisor.backends import Observation
from resource_advisor.contracts import Backend, JobRequest, State, now
from resource_advisor.store import jobs, outbox
from resource_advisor.worker import Worker


def submit_route(service, bundle, project, cluster, suffix, backend="slurm"):
    backend = Backend(backend)
    spec, candidate, variant, cap = bundle
    cap = cap.model_copy(
        update={"ref": "cap-" + suffix, "backend_cluster_id": cluster, "backend": backend}
    )
    variant = variant.model_copy(
        update={
            "ref": "variant-" + suffix,
            "project_ref": project,
            "workload_ref": "workload-" + suffix,
        }
    )
    candidate = candidate.model_copy(
        update={"variant_ref": variant.ref, "capability_ref": cap.ref, "backend": backend}
    )
    spec = spec.model_copy(
        update={"ref": variant.workload_ref, "project_ref": project, "candidates": (candidate,)}
    )
    for kind, model in [("workload", spec), ("variant", variant), ("capability", cap)]:
        service.register(kind, model, project)
    response = service.submit(
        project, JobRequest(workload_ref=spec.ref, candidate_ref=candidate.ref), "route-" + suffix
    )
    return response["job_id"]


def snapshot(service):
    with service.store.transaction() as conn:
        return {
            "jobs": [dict(r) for r in conn.execute(select(jobs).order_by(jobs.c.id)).mappings()],
            "events": [
                dict(r) for r in conn.execute(select(outbox).order_by(outbox.c.id)).mappings()
            ],
        }


def test_submit_skips_foreign_head_without_leasing_it(service, bundle):
    foreign = submit_route(service, bundle, "team-a", "hpc", "foreign")
    own = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "owned"
    )["job_id"]
    with service.store.transaction() as conn:
        conn.execute(
            update(outbox)
            .where(outbox.c.body["job_id"].as_string() == foreign)
            .values(id="000-foreign-head")
        )
    before = snapshot(service)
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    assert worker.submit_one()
    assert backend.submissions == 1
    assert service.get_job("team-a", own)["state"] == "QUEUED"
    assert service.get_job("team-a", foreign)["state"] == "VALIDATED"
    after = snapshot(service)
    assert before["events"][0] == after["events"][0]
    assert not worker.submit_one()
    hpc = SchedulerDouble()
    assert Worker(service, {("team-a", "hpc"): hpc}).submit_one()
    assert hpc.submissions == 1


def test_routes_match_pairs_not_independent_project_and_cluster_lists(service, bundle):
    foreign_a = submit_route(service, bundle, "team-a", "hpc", "a-hpc")
    foreign_b = submit_route(service, bundle, "team-b", "lab", "b-lab")
    own = submit_route(service, bundle, "team-b", "hpc", "b-hpc")
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend, ("team-b", "hpc"): backend})
    assert worker.submit_one()
    assert not worker.submit_one()
    assert service.get_job("team-b", own)["state"] == "QUEUED"
    assert service.get_job("team-a", foreign_a)["state"] == "VALIDATED"
    assert service.get_job("team-b", foreign_b)["state"] == "VALIDATED"
    assert backend.submissions == 1


@pytest.mark.parametrize("routes", [{}, {("team-b", "hpc"): SchedulerDouble()}])
def test_unowned_worker_does_not_cancel_expire_reconcile_or_release(service, bundle, routes):
    job_id = submit_route(service, bundle, "team-a", "hpc", "foreign")
    owner_backend = SchedulerDouble()
    owner = Worker(service, {("team-a", "hpc"): owner_backend})
    owner.submit_one()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job_id)
        service.store.change_job(
            conn,
            row,
            State.RUNNING,
            dict(row["body"], owner_lease_expires_at=(now() - timedelta(seconds=1)).isoformat()),
        )
        # Also exercise foreign cancellation/release events without invoking them.
        for kind in ("cancel", "release_termination"):
            service.store.enqueue(conn, kind + "-foreign", kind, {"job_id": job_id})
    before = snapshot(service)
    worker = Worker(service, routes)
    assert not worker.submit_one()  # Includes lease expiry scanning.
    worker.reconcile_all()
    assert not worker.cancel_one()
    assert not worker.release_one()
    assert snapshot(service) == before
    assert owner_backend.cancel_calls == 0
    owner.expire_owner_leases()
    assert service.get_job("team-a", job_id)["state"] == "CANCEL_REQUESTED"


def test_owned_cancellation_still_works_beside_another_route(service, bundle):
    job_id = submit_route(service, bundle, "team-a", "hpc", "cancel")
    backend = SchedulerDouble()
    owner = Worker(service, {("team-a", "hpc"): backend})
    owner.submit_one()
    service.cancel("team-a", job_id)
    unrelated = Worker(service, {("team-a", "lab"): SchedulerDouble()})
    assert not unrelated.cancel_one()
    assert owner.cancel_one()
    assert backend.cancel_calls == 1
    backend.observation = Observation(State.CANCELED)
    owner.reconcile_all()
    assert service.get_job("team-a", job_id)["state"] == "CANCELED"


def test_scoped_claim_ignores_orphans_and_reclaims_only_its_expired_lease(service, bundle):
    foreign = submit_route(service, bundle, "team-a", "hpc", "foreign")
    own = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "own"
    )["job_id"]
    event = service.store.claim("submit", job_routes={("team-a", "hpc")})
    assert event["body"]["job_id"] == foreign
    with service.store.transaction() as conn:
        conn.execute(
            update(outbox)
            .where(outbox.c.id == event["id"])
            .values(lease_until=(now() - timedelta(seconds=1)).isoformat())
        )
        service.store.enqueue(conn, "000-orphan", "submit", {"job_id": "missing"})
    claimed = service.store.claim("submit", job_routes={("team-a", "lab")})
    assert claimed["body"]["job_id"] == own
    assert not service.store.claim("submit", job_routes={("team-a", "lab")})
    reclaimed = service.store.claim("submit", job_routes={("team-a", "hpc")})
    assert reclaimed["id"] == event["id"] and reclaimed["tries"] == 2
    service.store.finish(event)  # Expired owner cannot finish the new lease.
    with service.store.transaction() as conn:
        row = conn.execute(select(outbox).where(outbox.c.id == event["id"])).mappings().one()
    assert row["status"] == "LEASED" and row["lease_token"] == reclaimed["lease_token"]


def test_postgres_concurrent_distinct_routes_claim_and_submit_once(service, bundle):
    if service.store.engine.dialect.name != "postgresql":
        pytest.skip("Concurrent transactions require the dedicated PostgreSQL test database")
    for suffix, cluster in [("one", "lab"), ("two", "hpc")]:
        submit_route(service, bundle, "team-a", cluster, suffix)
    backends = [SchedulerDouble(), SchedulerDouble()]
    workers = [
        Worker(service, {("team-a", cluster): backend})
        for cluster, backend in zip(("lab", "hpc"), backends, strict=True)
    ]
    barrier = Barrier(2)

    def submit(worker):
        barrier.wait(timeout=10)
        return worker.submit_one()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit, workers)) == [True, True]
    assert [b.submissions for b in backends] == [1, 1]
    assert not any(w.submit_one() for w in workers)
    assert {row["state"] for row in snapshot(service)["jobs"]} == {"QUEUED"}
