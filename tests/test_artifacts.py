import hashlib
import io
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_worker import setup

from resource_advisor.api import Principal, create_app
from resource_advisor.artifacts import ArtifactDelivery, ArtifactError, S3Artifacts
from resource_advisor.backends import Observation
from resource_advisor.contracts import State, now
from resource_advisor.store import outbox
from resource_advisor.worker import MLflowDelivery

errors = pytest.importorskip("botocore.exceptions")


class MemoryS3:
    """Conditional-write fault double, not evidence of a live object store."""

    def __init__(self):
        self.objects = {}
        self.writes = 0
        self.lose_put_response = False

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, **_):
        assert IfNoneMatch == "*"
        if (Bucket, Key) in self.objects:
            raise errors.ClientError({"ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        self.objects[Bucket, Key] = Body
        self.writes += 1
        if self.lose_put_response:
            self.lose_put_response = False
            raise errors.EndpointConnectionError(endpoint_url="https://s3.invalid")

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}


def test_content_addressed_bytes_are_verified_and_never_overwritten():
    s3 = MemoryS3()
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=s3)
    record = storage.put("team-a", "attempt-a", "job-a", {"value": 42})
    assert storage.put("team-a", "attempt-a", "job-a", {"value": 42}) == record
    assert s3.writes == 1
    assert json.loads(storage.read(record)) == {"value": 42}
    s3.objects[record["bucket"], record["key"]] = b'{"value":99}'
    with pytest.raises(ArtifactError, match="digest"):
        storage.read(record)
    with pytest.raises(ArtifactError, match="digest"):
        storage.put("team-a", "attempt-a", "job-a", {"value": 42})
    assert s3.writes == 1


@pytest.mark.parametrize("project,attempt", [("team-b", "a"), ("team-a", "../escape")])
def test_unmapped_owner_or_path_escape_rejected(project, attempt):
    s3 = MemoryS3()
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=s3)
    with pytest.raises(ArtifactError):
        storage.put(project, attempt, "job", {})
    assert s3.writes == 0


def test_location_change_does_not_silently_redirect_reads():
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=MemoryS3())
    record = storage.put("team-a", "attempt-a", "job-a", {})
    storage.buckets["team-a"] = "different-bucket"
    with pytest.raises(ArtifactError, match="configuration"):
        storage.read(record)


def test_put_response_loss_backfills_once_without_failing_compute_job(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    s3 = MemoryS3()
    s3.lose_put_response = True
    delivery = ArtifactDelivery(
        service.store, S3Artifacts(buckets={"team-a": "test-results"}, client=s3)
    )
    assert delivery.enqueue_existing() == 1
    assert delivery.deliver_one()
    with service.store.transaction() as conn:
        event = conn.execute(select(outbox).where(outbox.c.kind == "artifact")).mappings().one()
        assert event["status"] == "PENDING"
        assert not service.store.list(conn, "artifact", "team-a")
        conn.execute(
            update(outbox)
            .where(outbox.c.kind == "artifact")
            .values(lease_until=(now() - timedelta(seconds=1)).isoformat())
        )
    assert delivery.deliver_one()
    assert s3.writes == 1
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
    with service.store.transaction() as conn:
        assert len(service.store.list(conn, "artifact", "team-a")) == 1
        event = conn.execute(select(outbox).where(outbox.c.kind == "artifact")).mappings().one()
        assert event["status"] == "DONE"


def test_api_authorizes_artifact_download_and_checks_integrity(service):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    s3 = MemoryS3()
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=s3)
    assert ArtifactDelivery(service.store, storage).deliver_one()
    tokens = {hashlib.sha256(t.encode()).hexdigest(): Principal(t) for t in ["team-a", "team-b"]}
    with TestClient(create_app(service, tokens, artifact_storage=storage)) as client:
        headers = {"Authorization": "Bearer team-a"}
        listing = client.get(f"/api/v1/compute/jobs/{job['job_id']}/artifacts", headers=headers)
        assert listing.status_code == 200
        record = listing.json()[0]
        assert "bucket" not in record and "key" not in record
        path = f"/api/v1/compute/artifacts/{record['ref']}/content"
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["attempt_id"] == job["attempt_id"]
        assert response.headers["etag"] == '"' + record["digest"] + '"'
        assert client.get(path, headers={"Authorization": "Bearer team-b"}).status_code == 404
        s3.objects[next(iter(s3.objects))] = b"corrupted"
        assert client.get(path, headers=headers).status_code == 503


@pytest.mark.parametrize("fault", [None, "external_uri", "other_project", "corrupted"])
def test_mlflow_artifact_proxy_upload_verifies_ownership_and_bytes(service, fault):
    backend, worker, job = setup(service)
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=MemoryS3())
    ArtifactDelivery(service.store, storage).deliver_one()
    with service.store.transaction() as conn:
        service.store.put(
            conn,
            "tracking",
            job["attempt_id"],
            "team-a",
            {"job_id": job["job_id"], "run_id": "run-a", "experiment_id": "42"},
        )
    uploaded = b"tampered" if fault == "corrupted" else None
    writes = 0

    def server(request):
        nonlocal uploaded, writes
        assert request.url.host == "mlflow.invalid"
        if request.url.path.endswith("runs/get"):
            run = {
                "info": {
                    "experiment_id": "42",
                    "artifact_uri": "https://elsewhere.invalid/private"
                    if fault == "external_uri"
                    else "mlflow-artifacts:/42/run-a/artifacts",
                },
                "data": {
                    "tags": [
                        {
                            "key": "project",
                            "value": "team-b" if fault == "other_project" else "team-a",
                        },
                        {"key": "resource_advisor.attempt_id", "value": job["attempt_id"]},
                    ]
                },
            }
            return httpx.Response(200, json={"run": run})
        if request.method == "PUT":
            writes += 1
            uploaded = request.content
            return httpx.Response(200)
        return httpx.Response(404) if uploaded is None else httpx.Response(200, content=uploaded)

    with httpx.Client(
        base_url="https://mlflow.invalid", transport=httpx.MockTransport(server)
    ) as client:
        delivery = MLflowDelivery(
            service.store,
            "https://mlflow.invalid",
            experiments={"team-a": "42"},
            client=client,
            artifact_storage=storage,
        )
        delivery.enqueue_artifacts()
        assert delivery.deliver_artifact_one()
    with service.store.transaction() as conn:
        event = (
            conn.execute(select(outbox).where(outbox.c.kind == "mlflow_artifact")).mappings().one()
        )
        assert event["status"] == ("PENDING" if fault else "DONE")
    assert writes == (0 if fault else 1)
