import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_worker import setup

from resource_advisor.accounting import interval, summarize
from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation, slurm_allocation
from resource_advisor.contracts import State, now
from resource_advisor.store import usage


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
