import json
from datetime import timedelta

import httpx
from sqlalchemy import select, update

from resource_advisor.backends import BackendError, Observation, SubmissionUnknown
from resource_advisor.contracts import (
    ExecutionResult,
    JobRequest,
    Measurements,
    State,
    now,
    signature,
)
from resource_advisor.store import outbox
from resource_advisor.worker import MLflowDelivery, Worker


class SchedulerDouble:
    """Explicit fault-injection double; never reported as real scheduler evidence."""

    def __init__(self, *, lose_response=False):
        self.submissions = 0
        self.external_id = None
        self.lose_response = lose_response
        self.observation = Observation(State.QUEUED)
        self.cancel_calls = 0

    def submit(self, job):
        self.submissions += 1
        self.external_id = "42"
        if self.lose_response:
            raise SubmissionUnknown("response lost after acceptance")
        return self.external_id

    def reconcile(self, job):
        return self.external_id

    def status(self, job):
        return self.observation

    def cancel(self, job):
        self.cancel_calls += 1

    def result(self, job):
        b = job["body"]
        result = ExecutionResult(
            job_id=job["id"],
            attempt_id=b["attempt_id"],
            epoch=b["epoch"],
            workload_signature=b["workload_signature"],
            context_signature=b["context_signature"],
            evidence_kind="synthetic",
            outcome="COMPLETED",
            measurements=Measurements(
                elapsed_seconds=1,
                peak_memory_mib=100,
                quality_value=1,
                sample_count=10,
                work_units=10,
            ),
        )
        return {"result": result.model_dump(mode="json"), "digest": signature(result)}


def setup(service, **kwargs):
    backend = SchedulerDouble(**kwargs)
    worker = Worker(service, {("team-a", "lab"): backend})
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "one"
    )
    return backend, worker, job


def expire_retry(service):
    with service.store.transaction() as conn:
        conn.execute(
            update(outbox)
            .where(outbox.c.status == "PENDING")
            .values(lease_until=(now() - timedelta(seconds=1)).isoformat())
        )


def test_response_loss_reconciles_without_resubmission(service):
    backend, worker, job = setup(service, lose_response=True)
    worker.submit_one()
    assert service.get_job("team-a", job["job_id"])["state"] == "SUBMISSION_UNKNOWN"
    expire_retry(service)
    worker.submit_one()
    assert backend.submissions == 1
    assert service.get_job("team-a", job["job_id"])["state"] == "QUEUED"


def test_unknown_absence_is_not_permission_to_retry(service):
    backend, worker, job = setup(service, lose_response=True)
    worker.submit_one()
    backend.external_id = None
    expire_retry(service)
    worker.submit_one()
    assert backend.submissions == 1
    assert service.get_job("team-a", job["job_id"])["state"] == "SUBMISSION_UNKNOWN"


def test_cancel_before_submission_needs_no_backend(service):
    backend, worker, job = setup(service)
    service.cancel("team-a", job["job_id"])
    worker.submit_one()
    worker.cancel_one()
    assert backend.submissions == 0
    assert backend.cancel_calls == 0
    assert service.get_job("team-a", job["job_id"])["state"] == "CANCELED"


def test_cancel_ack_is_not_terminal_until_confirmed(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    service.cancel("team-a", job["job_id"])
    worker.cancel_one()
    worker.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == "CANCEL_REQUESTED"
    backend.observation = Observation(State.CANCELED)
    worker.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == "CANCELED"


def test_end_to_end_synthetic_result_and_usage(service):
    from resource_advisor.store import usage

    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(
        State.COLLECTING,
        (now() - timedelta(seconds=3)).isoformat(),
        now().isoformat(),
        allocation={"source": "test fixture", "accelerator_count": 1},
    )
    worker.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
    with service.store.transaction() as conn:
        record = conn.execute(select(usage)).mappings().one()
        assert record["allocated_device_seconds"] >= 3
        assert record["measured_compute_seconds"] == 1
        assert record["allocation_mode"] == "physical_device"


def test_collection_deadline_invalidates_missing_result(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        body = dict(row["body"], collecting_since=(now() - timedelta(seconds=301)).isoformat())
        service.store.change_job(conn, row, State.COLLECTING, body)
    worker.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == "RESULT_INVALID"


def test_backend_unavailable_preserves_job_and_records_error(service):
    backend, worker, job = setup(service)
    worker.submit_one()

    def unavailable(_):
        raise BackendError("disconnected")

    backend.status = unavailable
    worker.reconcile_all()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        assert row["state"] == "QUEUED"
        assert row["body"]["last_observation_error"] == "BackendError"


def test_mlflow_outage_is_durable_and_does_not_fail_job(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()

    def unavailable(request):
        return httpx.Response(503, request=request)

    client = httpx.Client(
        transport=httpx.MockTransport(unavailable), base_url="https://mlflow.invalid"
    )
    assert MLflowDelivery(
        service.store,
        "https://mlflow.invalid",
        experiments={"team-a": "team-a-experiment"},
        client=client,
    ).deliver_one()
    with service.store.transaction() as conn:
        row = conn.execute(select(outbox).where(outbox.c.kind == "mlflow")).mappings().one()
        assert row["status"] == "PENDING"
        assert row["tries"] == 1
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"


def test_claim_lease_prevents_duplicate_worker_claim(service):
    setup(service)
    assert service.store.claim("submit") is not None
    assert service.store.claim("submit") is None


def test_unmapped_project_cannot_write_to_mlflow_default_experiment(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    calls = []

    def unexpected(request):
        calls.append(request)
        return httpx.Response(500)

    with httpx.Client(
        transport=httpx.MockTransport(unexpected), base_url="https://mlflow.invalid"
    ) as client:
        delivery = MLflowDelivery(
            service.store,
            "https://mlflow.invalid",
            experiments={"other-project": "42"},
            client=client,
        )
        assert delivery.deliver_one()
    assert calls == []
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
    with service.store.transaction() as conn:
        event = conn.execute(select(outbox).where(outbox.c.kind == "mlflow")).mappings().one()
        assert event["status"] == "PENDING"


def test_mlflow_response_loss_reuses_run_and_measurement_timestamp(service):
    backend, worker, _ = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    runs, batches = [], []
    lose_response = True

    def server(request):
        nonlocal lose_response
        body = json.loads(request.content)
        if request.url.path.endswith("runs/search"):
            return httpx.Response(200, json={"runs": runs})
        if request.url.path.endswith("runs/create"):
            runs.append({"info": {"run_id": "external-run-1"}})
            return httpx.Response(200, json={"run": runs[-1]})
        if request.url.path.endswith("runs/log-batch"):
            batches.append(body)
            if lose_response:
                lose_response = False
                return httpx.Response(503)
        return httpx.Response(200, json={})

    with httpx.Client(
        transport=httpx.MockTransport(server), base_url="https://mlflow.invalid"
    ) as client:
        delivery = MLflowDelivery(
            service.store, "https://mlflow.invalid", experiments={"team-a": "42"}, client=client
        )
        delivery.deliver_one()
        with service.store.transaction() as conn:
            conn.execute(
                update(outbox)
                .where(outbox.c.kind == "mlflow")
                .values(lease_until=(now() - timedelta(seconds=1)).isoformat())
            )
        delivery.deliver_one()
    assert len(runs) == 1
    assert len(batches) == 2
    assert batches[0] == batches[1]
    with service.store.transaction() as conn:
        assert len(service.store.list(conn, "tracking", "team-a")) == 1
        event = conn.execute(select(outbox).where(outbox.c.kind == "mlflow")).mappings().one()
        assert event["status"] == "DONE"


def test_failed_event_backoff_does_not_block_next_submission(service):
    setup(service)
    event = service.store.claim("submit")
    service.store.finish(event, "BackendError")
    second = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "second"
    )
    claimed = service.store.claim("submit")
    assert claimed["body"]["job_id"] == second["job_id"]
