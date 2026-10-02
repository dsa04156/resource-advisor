"""Failure metadata recovery; scheduler doubles are not accelerator proof."""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import delete, select, update
from test_worker import setup

from resource_advisor.backends import Observation
from resource_advisor.contracts import State, now
from resource_advisor.store import jobs, outbox, usage
from resource_advisor.worker import MLflowDelivery


class TrackingServer:
    def __init__(self, *, lose_create=False):
        self.runs = []
        self.batches = []
        self.updates = []
        self.lose_create = lose_create

    def __call__(self, request):
        body = json.loads(request.content)
        path = request.url.path
        if path.endswith("runs/search"):
            return httpx.Response(200, json={"runs": self.runs})
        if path.endswith("runs/create"):
            run = {
                "info": {"run_id": "run-1", "experiment_id": body["experiment_id"]},
                "data": {"tags": body["tags"]},
            }
            self.runs.append(run)
            if self.lose_create:
                self.lose_create = False
                raise httpx.ReadTimeout("injected response loss", request=request)
            return httpx.Response(200, json={"run": run})
        if path.endswith("runs/log-batch"):
            self.batches.append(body)
        elif path.endswith("runs/update"):
            self.updates.append(body)
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={})


def delivery(service, server):
    return MLflowDelivery(
        service.store,
        "https://mlflow.invalid",
        experiments={"team-a": "42"},
        client=httpx.Client(
            base_url="https://mlflow.invalid", transport=httpx.MockTransport(server)
        ),
    )


def retry_now(service):
    with service.store.transaction() as conn:
        conn.execute(
            update(outbox)
            .where(outbox.c.kind == "mlflow")
            .values(lease_until=(now() - timedelta(seconds=1)).isoformat())
        )


@pytest.mark.parametrize(
    "state,external_status",
    [(State.FAILED, "FAILED"), (State.CANCELED, "KILLED"), (State.RESULT_INVALID, "FAILED")],
)
def test_resultless_terminal_attempt_tracks_metadata_without_measurements(
    service, state, external_status
):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(state, error="INJECTED_TEST_FAILURE")
    worker.reconcile_all()
    server = TrackingServer()
    assert delivery(service, server).deliver_one()
    assert server.batches[0]["metrics"] == []
    tags = {t["key"]: t["value"] for t in server.batches[0]["tags"]}
    assert tags["resource_advisor.state"] == state
    assert tags["resource_advisor.result_present"] == "false"
    assert tags["resource_advisor.result_valid"] == "false"
    assert tags["evidence_kind"] == "unavailable"
    assert tags["resource_advisor.error"] == "INJECTED_TEST_FAILURE"
    assert "result_digest" not in tags
    assert server.updates[0]["status"] == external_status
    with service.store.transaction() as conn:
        assert service.store.get(conn, "result", job["attempt_id"]) is None
        assert service.store.list(conn, "profile") == []
        assert service.store.list(conn, "artifact") == []
        assert len(service.store.list(conn, "tracking")) == 1


def test_invalid_result_is_not_logged_as_success_or_performance(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    original = backend.result

    def invalid(row):
        envelope = original(row)
        envelope["result"]["workload_signature"] = "sha256:" + "0" * 64
        return envelope

    backend.result = invalid
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == State.RESULT_INVALID
    server = TrackingServer()
    delivery(service, server).deliver_one()
    tags = {t["key"]: t["value"] for t in server.batches[0]["tags"]}
    assert tags["resource_advisor.result_present"] == "true"
    assert tags["resource_advisor.result_valid"] == "false"
    assert tags["quality_passed"] == "false"
    assert tags["evidence_kind"] == "unavailable"
    assert tags["workload_signature"] == job["workload_signature"]
    assert server.batches[0]["metrics"] == []
    assert server.updates[0]["status"] == "FAILED"


def test_tracking_event_and_usage_roll_back_with_terminal_transition(service):
    _, _, job = setup(service)
    with pytest.raises(RuntimeError), service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.FAILED, row["body"])
        raise RuntimeError("transaction rollback")
    with service.store.transaction() as conn:
        assert service.store.job(conn, job["job_id"])["state"] == State.VALIDATED
        assert conn.execute(select(usage)).first() is None
        assert conn.execute(select(outbox).where(outbox.c.kind == "mlflow")).first() is None


def test_tracking_backfill_is_missing_only_and_does_not_invent_results(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.FAILED)
    worker.reconcile_all()
    with service.store.transaction() as conn:
        conn.execute(delete(outbox).where(outbox.c.kind == "mlflow"))
        original = dict(service.store.job(conn, job["job_id"]))
    assert service.store.backfill_tracking() == 1
    assert service.store.backfill_tracking() == 0
    with service.store.transaction() as conn:
        assert dict(service.store.job(conn, job["job_id"])) == original
        assert service.store.get(conn, "result", job["attempt_id"]) is None


def test_create_response_loss_recovers_same_failure_run(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.FAILED)
    worker.reconcile_all()
    server = TrackingServer(lose_create=True)
    runner = delivery(service, server)
    assert runner.deliver_one()
    with service.store.transaction() as conn:
        event = conn.execute(select(outbox).where(outbox.c.kind == "mlflow")).mappings().one()
        assert event["status"] == "PENDING"
        assert service.store.get(conn, "tracking", job["attempt_id"]) is None
    retry_now(service)
    assert runner.deliver_one()
    assert len(server.runs) == 1
    assert server.updates[0]["status"] == "FAILED"
    assert not runner.deliver_one()


def test_legacy_missing_terminal_timestamp_stays_unknown(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.CANCELED)
    worker.reconcile_all()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        body = dict(row["body"])
        del body["finished_at"]
        conn.execute(update(jobs).where(jobs.c.id == row["id"]).values(body=body))
    server = TrackingServer()
    delivery(service, server).deliver_one()
    assert server.updates[0]["status"] == "KILLED"
    assert "end_time" not in server.updates[0]
    tags = {t["key"]: t["value"] for t in server.batches[0]["tags"]}
    assert tags["resource_advisor.end_time_source"] == "unknown_legacy"
    with service.store.transaction() as conn:
        assert "finished_at" not in service.store.job(conn, job["job_id"])["body"]


@pytest.mark.parametrize("fault", ["project", "attempt", "job", "experiment", "duplicate"])
def test_foreign_or_ambiguous_search_result_never_modified(service, fault):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.FAILED)
    worker.reconcile_all()
    server = TrackingServer(lose_create=True)
    runner = delivery(service, server)
    runner.deliver_one()
    if fault == "duplicate":
        server.runs.append(server.runs[0])
    elif fault == "experiment":
        server.runs[0]["info"]["experiment_id"] = "other"
    else:
        key = {
            "project": "project",
            "attempt": "resource_advisor.attempt_id",
            "job": "resource_advisor.job_id",
        }[fault]
        for tag in server.runs[0]["data"]["tags"]:
            if tag["key"] == key:
                tag["value"] = "other"
    retry_now(service)
    runner.deliver_one()
    assert not server.batches and not server.updates
    with service.store.transaction() as conn:
        assert service.store.get(conn, "tracking", job["attempt_id"]) is None
        event = conn.execute(select(outbox).where(outbox.c.kind == "mlflow")).mappings().one()
        assert event["status"] == "PENDING"
