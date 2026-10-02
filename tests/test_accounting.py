import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from test_worker import setup

from resource_advisor.accounting import interval, summarize
from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation, slurm_allocation
from resource_advisor.contracts import State, now
from resource_advisor.store import jobs, usage


def record(service):
    with service.store.transaction() as conn:
        return dict(conn.execute(select(usage)).mappings().one())


@pytest.mark.parametrize("state", [State.FAILED, State.CANCELED, State.RESULT_INVALID])
def test_resultless_terminal_usage_is_atomic_and_idempotent(service, state):
    backend, worker, job = setup(service)
    worker.submit_one()
    end = now()
    backend.observation = Observation(
        state,
        (end - timedelta(seconds=7)).isoformat(),
        end.isoformat(),
        allocation={"source": "fixture", "accelerator_count": 2, "cpu": 3, "memory_mib": 512},
        submitted_at=(end - timedelta(seconds=11)).isoformat(),
    )
    worker.reconcile_all()
    worker.reconcile_all()
    r = record(service)
    assert r["body"]["outcome"] == state
    assert r["allocated_device_seconds"] == 14  # Actual allocation, not requested one GPU.
    assert r["body"]["allocated_cpu_seconds"] == 21
    assert r["queue_seconds"] == 4
    assert r["measured_compute_seconds"] is None
    assert r["body"]["terminal_observed_at"]
    assert service.store.backfill_usage() == 0
    with service.store.transaction() as conn:
        assert not service.store.list(conn, "profile")
        assert not service.store.list(conn, "result")


def test_missing_cancellation_timestamps_do_not_mean_zero(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    service.cancel("team-a", job["job_id"])
    worker.cancel_one()
    backend.observation = Observation(State.CANCELED)
    worker.reconcile_all()
    r = record(service)
    assert r["allocated_device_seconds"] is None
    assert "ALLOCATION_INTERVAL_UNKNOWN" in r["body"]["uncertainty"]
    assert not r["body"]["never_submitted"]
    assert r["body"]["cancel_requested_at"]
    assert r["body"]["cancel_dispatch_started_at"]
    assert r["body"]["cancel_acknowledged_at"]
    assert r["body"]["cancel_confirmed_at"] == r["body"]["terminal_observed_at"]
    assert r["body"]["cancel_confirmation_seconds"] >= 0
    assert r["body"]["backend_finished_at"] is None


def test_cancel_response_loss_preserves_intent_and_observed_allocation(service):
    from test_worker import expire_retry

    from resource_advisor.backends import BackendError
    from resource_advisor.worker import Worker

    backend, worker, job = setup(service)
    worker.submit_one()
    t = now()
    backend.observation = Observation(
        State.RUNNING,
        (t - timedelta(seconds=10)).isoformat(),
        allocation={"source": "fixture", "accelerator_count": 1, "cpu": 2},
        submitted_at=(t - timedelta(seconds=20)).isoformat(),
    )
    first = service.cancel("team-a", job["job_id"])
    second = service.cancel("team-a", job["job_id"])
    assert first["cancel_requested_at"] == second["cancel_requested_at"]

    def accepted_but_lost(_):
        backend.cancel_calls += 1
        backend.observation = Observation(State.CANCELED)
        raise BackendError("accepted deletion; response lost")

    backend.cancel = accepted_but_lost
    worker.cancel_one()
    interrupted = service.get_job("team-a", job["job_id"])
    assert interrupted["state"] == "CANCEL_REQUESTED"
    assert interrupted["cancel_dispatch_started_at"]
    assert interrupted["cancel_acknowledged_at"] is None
    expire_retry(service)
    restarted = Worker(service, worker.backends)
    restarted.cancel_one()
    assert backend.cancel_calls == 1  # Re-observe the completed cancel, do not delete again.
    r = record(service)
    assert r["body"]["cancel_requested_at"] == first["cancel_requested_at"]
    assert r["body"]["cancel_dispatch_started_at"] == interrupted["cancel_dispatch_started_at"]
    assert r["body"]["cancel_acknowledged_at"] is None
    assert r["body"]["observed_allocation"]["accelerator_count"] == 1
    assert r["queue_seconds"] == 10
    assert r["allocated_device_seconds"] is None  # Disappearance supplies no finish timestamp.
    assert r["body"]["cancel_confirmation_seconds"] >= 0
    restarted.reconcile_all()
    assert service.store.backfill_usage() == 0


def test_cancel_before_submit_proves_zero_and_backfills_once(service):
    _, worker, job = setup(service)
    service.cancel("team-a", job["job_id"])
    worker.cancel_one()
    assert record(service)["allocated_device_seconds"] == 0
    with service.store.transaction() as conn:
        conn.execute(delete(usage))  # Simulate a pre-upgrade missing ledger row.
    assert service.store.backfill_usage() == 1
    assert service.store.backfill_usage() == 0
    assert record(service)["body"]["never_submitted"]
    assert record(service)["body"]["cancel_requested_at"]
    assert record(service)["body"]["cancel_dispatch_started_at"] is None
    assert record(service)["body"]["cancel_acknowledged_at"] is None
    assert record(service)["body"]["cancel_confirmation_seconds"] >= 0


def test_legacy_pending_cancel_does_not_invent_original_request_time(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    with service.store.transaction() as conn:
        conn.execute(
            update(jobs).where(jobs.c.id == job["job_id"]).values(state="CANCEL_REQUESTED")
        )  # Existing pre-upgrade request without a recorded timestamp.
    service.cancel("team-a", job["job_id"])
    worker.cancel_one()
    backend.observation = Observation(State.CANCELED)
    worker.reconcile_all()
    r = record(service)
    assert r["body"]["cancel_requested_at"] is None
    assert r["body"]["cancel_confirmation_seconds"] is None
    assert r["body"]["cancel_dispatch_started_at"]


def test_summary_preserves_unknown_and_device_units(service):
    _, worker, job = setup(service)
    service.cancel("team-a", job["job_id"])
    worker.cancel_one()
    a = record(service)
    b = dict(a, allocation_mode="virtual_slot", allocated_device_seconds=99)
    c = dict(a, allocated_device_seconds=999, body=dict(a["body"], schema_version="v1"))
    groups = summarize([a, b, c])
    assert len(groups) == 2
    physical = next(g for g in groups if g["allocation_mode"] == "physical_device")
    assert physical["known_allocated_device_seconds"] == 0
    assert physical["unknown_allocation_attempts"] == 1
    assert physical["legacy_unqualified_attempts"] == 1
    different_model = dict(a, body=dict(a["body"], accelerator_model="other-model"))
    assert len(summarize([a, different_model])) == 2
    tokens = {
        hashlib.sha256(t.encode()).hexdigest(): Principal(p)
        for t, p in [("token-a", "team-a"), ("token-b", "team-b")]
    }
    client = TestClient(create_app(service, tokens))
    path = "/api/v1/compute/usage/summary"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer token-b"}).json()["groups"] == []
    assert (
        client.get(path, headers={"Authorization": "Bearer token-a"}).json()["groups"][0][
            "attempts"
        ]
        == 1
    )


def test_accounting_does_not_repair_invalid_time_or_double_count_typed_gres():
    assert interval("2026-10-02T01:00:00", "2026-10-02T01:00:03") is None
    assert interval("2026-10-02T01:00:03+00:00", "2026-10-02T01:00:00+00:00") is None
    allocated = slurm_allocation("cpu=2,mem=2G,gres/gpu=1,gres/gpu:test=1", "gpu:test")
    assert allocated["accelerator_count"] == 1
    assert allocated["memory_mib"] == 2048
    assert slurm_allocation("", "gpu:test") is None
