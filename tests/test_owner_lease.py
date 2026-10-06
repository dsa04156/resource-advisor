import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation
from resource_advisor.contracts import JobRequest, State, now, signature
from resource_advisor.service import NotFound, Rejected
from resource_advisor.store import Conflict, jobs, usage
from resource_advisor.worker import Worker


@pytest.fixture
def lease_clock(monkeypatch):
    clock = [now()]
    monkeypatch.setattr("resource_advisor.service.now", lambda: clock[0])
    monkeypatch.setattr("resource_advisor.worker.now", lambda: clock[0])
    return clock


def owned(service, *, lose_response=False):
    backend = SchedulerDouble(lose_response=lose_response)
    worker = Worker(service, {("team-a", "lab"): backend})
    job = service.submit(
        "team-a",
        JobRequest(workload_ref="workload-1", candidate_ref="base", owner_lease_seconds=60),
        "owned",
    )
    return backend, worker, job


def test_heartbeat_extends_opt_in_lease_and_replay_does_not(service, lease_clock):
    _, worker, job = owned(service)
    worker.submit_one()
    lease_clock[0] += timedelta(seconds=30)
    refreshed = service.heartbeat("team-a", job["job_id"])
    assert refreshed["owner_lease_expires_at"] > job["owner_lease_expires_at"]
    lease_clock[0] += timedelta(seconds=31)
    worker.expire_owner_leases()
    assert service.get_job("team-a", job["job_id"])["state"] == "QUEUED"
    replay = service.submit(
        "team-a",
        JobRequest(workload_ref="workload-1", candidate_ref="base", owner_lease_seconds=60),
        "owned",
    )
    assert replay["owner_lease_expires_at"] == refreshed["owner_lease_expires_at"]


def test_expired_owner_cancels_before_submit_without_allocating(service, lease_clock):
    backend, worker, job = owned(service)
    lease_clock[0] += timedelta(seconds=61)
    worker.submit_one()
    worker.cancel_one()
    current = service.get_job("team-a", job["job_id"])
    assert current["state"] == "CANCELED"
    assert current["error"] == "OWNER_LEASE_EXPIRED"
    assert backend.submissions == backend.cancel_calls == 0
    with service.store.transaction() as conn:
        assert conn.execute(select(usage.c.allocated_device_seconds)).scalar_one() == 0


def test_late_heartbeat_cannot_revive_expired_or_cancelled_owner(service, lease_clock):
    backend, worker, job = owned(service)
    worker.submit_one()
    lease_clock[0] += timedelta(seconds=60)
    expired = service.heartbeat("team-a", job["job_id"])
    assert expired["state"] == "CANCEL_REQUESTED"
    assert expired["owner_lease_expires_at"] == job["owner_lease_expires_at"]
    worker.cancel_one()
    backend.observation = Observation(State.CANCELED)
    worker.reconcile_all()
    assert service.heartbeat("team-a", job["job_id"])["state"] == "CANCELED"


def test_lost_submit_response_still_has_durable_expiry_and_single_submission(service, lease_clock):
    backend, worker, job = owned(service, lose_response=True)
    worker.submit_one()
    assert service.get_job("team-a", job["job_id"])["state"] == "SUBMISSION_UNKNOWN"
    lease_clock[0] += timedelta(seconds=61)
    recovered = Worker(service, worker.backends)
    recovered.expire_owner_leases()
    recovered.cancel_one()
    assert backend.submissions == backend.cancel_calls == 1
    backend.observation = Observation(State.CANCELED)
    recovered.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == "CANCELED"


def test_owner_loss_never_cancels_ordinary_job_or_completed_collection(service, lease_clock):
    backend, worker, job = owned(service)
    worker.submit_one()
    ordinary = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "ordinary"
    )
    worker.submit_one()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.COLLECTING, row["body"])
    lease_clock[0] += timedelta(seconds=61)
    worker.expire_owner_leases()
    assert service.get_job("team-a", job["job_id"])["state"] == "COLLECTING"
    assert service.get_job("team-a", ordinary["job_id"])["state"] == "QUEUED"
    with pytest.raises(Rejected):
        service.heartbeat("team-a", ordinary["job_id"])
    with pytest.raises(NotFound):
        service.heartbeat("team-b", job["job_id"])
    assert backend.cancel_calls == 0


def test_heartbeat_api_requires_same_project(service):
    _, _, job = owned(service)
    tokens = {
        hashlib.sha256(t.encode()).hexdigest(): Principal(p)
        for t, p in [("a", "team-a"), ("b", "team-b")]
    }
    client = TestClient(create_app(service, tokens))
    path = "/api/v1/compute/jobs/" + job["job_id"] + "/heartbeat"
    assert client.post(path).status_code == 401
    assert client.post(path, headers={"Authorization": "Bearer b"}).status_code == 404
    assert client.post(path, headers={"Authorization": "Bearer a"}).status_code == 200


def test_default_request_keeps_old_idempotency_digest(service):
    request = JobRequest(workload_ref="workload-1", candidate_ref="base")
    # Freeze the actual pre-lease request contract. Later optional profile/template
    # fields must not silently become part of this historical fixture.
    legacy = {
        "workload_ref": "workload-1",
        "candidate_ref": "base",
        "mode": "observe",
        "approval_ref": None,
        "study_ref": None,
        "parent_run_ref": None,
        "probe_plan_ref": None,
    }
    job = service.submit("team-a", request, "legacy-key")
    with service.store.transaction() as conn:
        row = conn.execute(select(jobs).where(jobs.c.id == job["job_id"])).mappings().one()
        assert row["request_digest"] == signature(legacy)
    assert service.submit("team-a", request, "legacy-key")["job_id"] == job["job_id"]
    with pytest.raises(Conflict):
        service.submit(
            "team-a", request.model_copy(update={"owner_lease_seconds": 60}), "legacy-key"
        )


@pytest.mark.parametrize("seconds", [0, 14, 301, True, 30.5])
def test_owner_lease_bounds(seconds):
    with pytest.raises(ValidationError):
        JobRequest(workload_ref="w", candidate_ref="c", owner_lease_seconds=seconds)
