import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event
from test_artifacts import MemoryS3
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.artifacts import ArtifactDelivery, S3Artifacts
from resource_advisor.backends import Observation
from resource_advisor.contracts import State, WorkloadSpec, now, signature
from resource_advisor.model_lifecycle import LifecyclePolicy
from resource_advisor.training import TrainingIsolation
from resource_advisor.worker import MLflowDelivery, Worker


def lifecycle_fixture(service, bundle, *, maximum_mse=1.0, policy_ref="linear-policy"):
    spec, candidate, variant, _ = bundle
    training_input = {
        "x": [[i / 8, ((i * 3) % 7) / 7] for i in range(8)],
        "y": [[2 * (i / 8) - 3 * (((i * 3) % 7) / 7) + 0.5] for i in range(8)],
    }
    cp_digest = signature({"fixture": "initial-linear-checkpoint"})
    identity = spec.identity.model_copy(
        update={
            "task_type": "training",
            "model_digest": cp_digest,
            "dataset_version": signature(training_input),
            "input_shape": (8, 2),
            "measurement_boundary": "linear-sgd-ten-step-synchronized-v1",
        }
    )
    variant = variant.model_copy(
        update={
            "ref": "lifecycle-train-variant",
            "workload_ref": "lifecycle-train-workload",
            "model_digest": cp_digest,
            "workload_signature": signature(identity),
            "verification": "TRAINING_VERIFIED",
            "command": ("python", "-m", "resource_advisor.gpu_training"),
            "supported_shapes": ((8, 2),),
        }
    )
    candidate = candidate.model_copy(update={"variant_ref": variant.ref})
    spec = WorkloadSpec.model_validate(
        spec.model_copy(
            update={
                "ref": "lifecycle-train-workload",
                "identity": identity,
                "candidates": (candidate,),
                "profiling": spec.profiling.model_copy(update={"checkpoint_digest": cp_digest}),
            }
        ).model_dump()
    )
    binding = TrainingIsolation(
        ref=spec.ref,
        project_ref="team-a",
        workload_ref=spec.ref,
        workload_digest=signature(spec),
        checkpoint_digest=cp_digest,
        input_digest=signature(training_input),
        checkpoint_size_bytes=100,
        input_size_bytes=300,
        validation_refs=("qualified-linear-fixture",),
    )
    for kind, model in [("variant", variant), ("workload", spec), ("training_isolation", binding)]:
        service.register(kind, model, "team-a")
    policy = LifecyclePolicy(
        ref=policy_ref,
        project_ref="team-a",
        workload_ref=spec.ref,
        candidate_ref=candidate.ref,
        workload_digest=signature(spec),
        model_name="ra-team-a-linear",
        training_input=training_input,
        holdout={"x": [[0.2, 0.3], [0.6, 0.1]], "y": [[0.0], [1.4]]},
        maximum_mse=maximum_mse,
    )
    client = TestClient(
        create_app(
            service,
            {
                hashlib.sha256(b"operator").hexdigest(): Principal("team-a", True),
                hashlib.sha256(b"user").hexdigest(): Principal("team-a"),
                hashlib.sha256(b"other").hexdigest(): Principal("team-b", True),
            },
        )
    )
    return client, policy, binding


def test_start_freezes_policy_and_reuses_training_job(service, bundle):
    client, policy, _ = lifecycle_fixture(service, bundle)
    registered = client.post(
        "/api/v1/compute/model-lifecycle-policies",
        headers={"Authorization": "Bearer operator"},
        json=policy.model_dump(mode="json"),
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": "Bearer user", "Idempotency-Key": "one-model"}
    started = client.post(
        "/api/v1/compute/model-lifecycles", headers=headers, json={"policy_ref": policy.ref}
    )
    assert started.status_code == 200, started.text
    first = started.json()
    assert first["state"] == "TRAINING"
    replay = client.post(
        "/api/v1/compute/model-lifecycles", headers=headers, json={"policy_ref": policy.ref}
    ).json()
    assert replay["job_id"] == first["job_id"]
    assert replay["attempt_id"] == first["attempt_id"]
    assert len(client.get("/api/v1/compute/jobs", headers=headers).json()) == 1
    assert (
        client.get(
            "/api/v1/compute/model-lifecycles/" + first["ref"],
            headers={"Authorization": "Bearer other"},
        ).status_code
        == 404
    )


class TrainingScheduler(SchedulerDouble):
    """Fake scheduler envelopes exercise contracts; they do not qualify hardware."""

    def __init__(self, binding, *, evidence_kind="hardware", bias=0.75):
        super().__init__()
        self.binding, self.evidence_kind, self.bias = binding, evidence_kind, bias

    def result(self, job):
        envelope = super().result(job)
        result = dict(envelope["result"], evidence_kind=self.evidence_kind)
        checkpoint = {
            "weights": [[2.0], [-3.0]],
            "bias": [self.bias],
            "momentum_weights": [[0.0], [0.0]],
            "momentum_bias": [0.0],
            "step": 10,
            "lr": 0.05,
            "momentum": 0.9,
        }
        receipt = {
            "job_id": job["id"],
            "attempt_id": job["body"]["attempt_id"],
            "result_digest": signature(result),
            "initial_checkpoint_digest": self.binding.checkpoint_digest,
            "checkpoint_after_digest": self.binding.checkpoint_digest,
            "input_digest": self.binding.input_digest,
            "input_after_digest": self.binding.input_digest,
            "checkpoint_readonly": True,
            "input_readonly": True,
            "source_not_mounted": True,
            "output_checkpoint": checkpoint,
            "output_checkpoint_digest": signature(checkpoint),
        }
        return {"result": result, "digest": signature(result), "training_receipt": receipt}


def finish_training(service, client, policy, binding, *, key="pipeline", **scheduler_options):
    assert (
        client.post(
            "/api/v1/compute/model-lifecycle-policies",
            headers={"Authorization": "Bearer operator"},
            json=policy.model_dump(mode="json"),
        ).status_code
        == 200
    )
    started = client.post(
        "/api/v1/compute/model-lifecycles",
        headers={"Authorization": "Bearer user", "Idempotency-Key": key},
        json={"policy_ref": policy.ref},
    ).json()
    backend = TrainingScheduler(binding, **scheduler_options)
    worker = Worker(service, {("team-a", "lab"): backend})
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=MemoryS3())
    assert ArtifactDelivery(service.store, storage).deliver_one()
    return started, storage


def test_independent_holdout_failure_preserves_evaluation_and_blocks_registration(service, bundle):
    from resource_advisor.model_lifecycle import LifecycleDelivery

    client, policy, binding = lifecycle_fixture(service, bundle, maximum_mse=0.01)
    started, storage = finish_training(service, client, policy, binding)
    delivery = LifecycleDelivery(service, storage, registry=None)
    assert delivery.deliver_one()
    observed = client.get(
        "/api/v1/compute/model-lifecycles/" + started["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    assert observed["state"] == "QUALITY_REJECTED"
    assert abs(observed["evaluation"]["mse"] - 0.0625) < 1e-12
    assert observed["evaluation"]["passed"] is False
    assert observed["evaluation"]["sample_count"] == 2
    assert observed["evaluation"]["training_attempt_id"] == started["attempt_id"]
    assert observed["registration"] is None
    assert observed["training_state"] == "SUCCEEDED"
    assert delivery.deliver_one() is False


class RegistryServer:
    """External MLflow REST double with real artifact bytes and version identity."""

    def __init__(self):
        self.run = None
        self.model = None
        self.versions = []
        self.files = {}
        self.creates = 0
        self.lose_version_response = False
        self.drop_version_request = False

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.method == "POST" else {}
        if path.endswith("runs/search"):
            return httpx.Response(200, json={"runs": [self.run] if self.run else []})
        if path.endswith("runs/create"):
            self.run = {
                "info": {
                    "run_id": "1" * 32,
                    "experiment_id": "123",
                    "status": "FINISHED",
                    "artifact_uri": "mlflow-artifacts:/123/" + "1" * 32 + "/artifacts",
                },
                "data": {"tags": body["tags"]},
            }
            return httpx.Response(200, json={"run": self.run})
        if path.endswith("runs/get"):
            return httpx.Response(200, json={"run": self.run})
        if path.endswith(("runs/log-batch", "runs/update")):
            return httpx.Response(200, json={})
        if "/mlflow-artifacts/artifacts/" in path:
            if request.method == "PUT":
                self.files[path] = request.content
                return httpx.Response(200)
            return (
                httpx.Response(200, content=self.files[path])
                if path in self.files
                else httpx.Response(404)
            )
        if path.endswith("registered-models/get"):
            return (
                httpx.Response(200, json={"registered_model": self.model})
                if self.model
                else httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
            )
        if path.endswith("registered-models/create"):
            self.model = {"name": body["name"], "tags": body["tags"]}
            return httpx.Response(200, json={"registered_model": self.model})
        if path.endswith("model-versions/search"):
            return httpx.Response(200, json={"model_versions": self.versions})
        if path.endswith("model-versions/create"):
            self.creates += 1
            if self.drop_version_request:
                raise httpx.ReadTimeout("request outcome unknown", request=request)
            version = dict(body, version=str(len(self.versions) + 1), status="READY")
            self.versions.append(version)
            if self.lose_version_response:
                raise httpx.ReadTimeout("response lost after commit", request=request)
            return httpx.Response(200, json={"model_version": version})
        raise AssertionError("unexpected MLflow operation: " + str(request.url))


def registry_delivery(service, storage, server):
    from resource_advisor.model_lifecycle import LifecycleDelivery
    from resource_advisor.model_registry import MLflowModelRegistry

    mlflow = MLflowDelivery(
        service.store,
        "https://mlflow.invalid",
        experiments={"team-a": "123"},
        client=httpx.Client(
            base_url="https://mlflow.invalid", transport=httpx.MockTransport(server.handle)
        ),
    )
    assert mlflow.deliver_one()
    return LifecycleDelivery(service, storage, MLflowModelRegistry(service.store, mlflow))


def test_quality_pass_registers_explicit_version_and_verified_predictable_weights(service, bundle):
    from resource_advisor.linear_model import LinearModel

    client, policy, binding = lifecycle_fixture(service, bundle)
    started, storage = finish_training(service, client, policy, binding)
    server = RegistryServer()
    delivery = registry_delivery(service, storage, server)
    assert delivery.deliver_one()
    observed = client.get(
        "/api/v1/compute/model-lifecycles/" + started["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    assert observed["state"] == "REGISTERED", observed
    registered = observed["registration"]
    assert registered["model_uri"] == "models:/ra-team-a-linear/1"
    assert registered["run_id"] == "1" * 32
    checkpoint_bytes = next(
        v for k, v in server.files.items() if k.endswith("data/checkpoint.json")
    )
    predictions = LinearModel(json.loads(checkpoint_bytes)).predict([[0.2, 0.3], [0.6, 0.1]])
    assert abs(predictions[0][0] - 0.25) < 1e-12
    assert abs(predictions[1][0] - 1.65) < 1e-12
    assert (
        registered["output_checkpoint_digest"] == observed["evaluation"]["output_checkpoint_digest"]
    )
    assert delivery.deliver_one() is False
    assert server.creates == 1


def test_lost_registry_response_reconciles_existing_version_without_second_create(
    service, bundle, monkeypatch
):
    client, policy, binding = lifecycle_fixture(service, bundle)
    started, storage = finish_training(service, client, policy, binding)
    server = RegistryServer()
    server.lose_version_response = True
    delivery = registry_delivery(service, storage, server)
    assert delivery.deliver_one()
    first = client.get(
        "/api/v1/compute/model-lifecycles/" + started["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    assert first["state"] == "REGISTRATION_PENDING"
    advanced = now() + timedelta(seconds=400)
    monkeypatch.setattr("resource_advisor.store.now", lambda: advanced)
    assert delivery.deliver_one()
    reconciled = client.get(
        "/api/v1/compute/model-lifecycles/" + started["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    assert reconciled["state"] == "REGISTERED"
    assert reconciled["registration"]["model_version"] == "1"
    assert server.creates == 1


def test_uncertain_version_creation_blocks_automatic_second_post(service, bundle, monkeypatch):
    client, policy, binding = lifecycle_fixture(service, bundle)
    started, storage = finish_training(service, client, policy, binding)
    server = RegistryServer()
    server.drop_version_request = True
    delivery = registry_delivery(service, storage, server)
    assert delivery.deliver_one()
    advanced = now() + timedelta(seconds=400)
    monkeypatch.setattr("resource_advisor.store.now", lambda: advanced)
    assert delivery.deliver_one()
    blocked = client.get(
        "/api/v1/compute/model-lifecycles/" + started["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    assert blocked["state"] == "REGISTRY_RECONCILIATION_REQUIRED"
    assert blocked["registration"] is None
    assert server.creates == 1
    assert server.versions == []


def test_changed_evaluator_is_rejected_before_training_submission(service, bundle):
    client, policy, _ = lifecycle_fixture(service, bundle)
    changed = policy.model_dump(mode="json")
    changed["evaluator_source_digest"] = "sha256:" + "0" * 64
    response = client.post(
        "/api/v1/compute/model-lifecycle-policies",
        headers={"Authorization": "Bearer operator"},
        json=changed,
    )
    assert response.status_code == 422
    assert "evaluator" in response.json()["detail"]
    assert client.get("/api/v1/compute/jobs", headers={"Authorization": "Bearer user"}).json() == []


@pytest.mark.parametrize("fault", ["synthetic", "corrupted"])
def test_unqualified_or_corrupt_training_artifact_never_reaches_registry(service, bundle, fault):
    client, policy, binding = lifecycle_fixture(service, bundle)
    started, storage = finish_training(
        service,
        client,
        policy,
        binding,
        evidence_kind="synthetic" if fault == "synthetic" else "hardware",
    )
    if fault == "corrupted":
        storage.client.objects[next(iter(storage.client.objects))] = b"tampered checkpoint"
    server = RegistryServer()
    delivery = registry_delivery(service, storage, server)
    assert delivery.deliver_one()
    observed = client.get(
        "/api/v1/compute/model-lifecycles/" + started["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    assert observed["evaluation"] is None
    assert observed["registration"] is None
    assert observed["delivery_error"]
    assert observed["training_state"] == "SUCCEEDED"
    assert server.creates == 0


@pytest.mark.parametrize(
    "fault", ["foreign_model", "foreign_run", "external_uri", "file_corruption"]
)
def test_registry_owner_and_artifact_integrity_fail_closed(service, bundle, fault):
    client, policy, binding = lifecycle_fixture(service, bundle)
    started, storage = finish_training(service, client, policy, binding)
    server = RegistryServer()
    delivery = registry_delivery(service, storage, server)
    if fault == "foreign_model":
        server.model = {
            "name": policy.model_name,
            "tags": [{"key": "resource_advisor.project", "value": "team-b"}],
        }
    elif fault == "foreign_run":
        server.run["data"]["tags"] = [{"key": "project", "value": "team-b"}]
    elif fault == "external_uri":
        server.run["info"]["artifact_uri"] = "https://foreign.invalid/model"
    else:
        original = server.handle

        def corrupt(request):
            if request.method == "GET" and request.url.path.endswith("data/checkpoint.json"):
                return httpx.Response(200, content=b"corrupt")
            return original(request)

        delivery.registry.client = httpx.Client(
            base_url="https://mlflow.invalid", transport=httpx.MockTransport(corrupt)
        )
    assert delivery.deliver_one()
    observed = client.get(
        "/api/v1/compute/model-lifecycles/" + started["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    assert observed["registration"] is None
    assert observed["delivery_error"] == "ArtifactError"
    assert server.creates == 0


@pytest.mark.parametrize(
    "fault", ["nonoperator", "overlap", "dataset_mismatch", "changed_threshold"]
)
def test_policy_qualification_and_immutability(service, bundle, fault):
    client, policy, _ = lifecycle_fixture(service, bundle)
    request = policy.model_dump(mode="json")
    headers = {"Authorization": "Bearer operator"}
    expected = 422
    if fault == "nonoperator":
        headers = {"Authorization": "Bearer user"}
        expected = 403
    elif fault == "overlap":
        request["holdout"]["x"][0] = request["training_input"]["x"][0]
    elif fault == "dataset_mismatch":
        request["training_input"]["y"][0][0] += 1
    else:
        assert (
            client.post(
                "/api/v1/compute/model-lifecycle-policies", headers=headers, json=request
            ).status_code
            == 200
        )
        request["maximum_mse"] = 0.0
        expected = 409
    assert (
        client.post(
            "/api/v1/compute/model-lifecycle-policies", headers=headers, json=request
        ).status_code
        == expected
    )
    assert client.get("/api/v1/compute/jobs", headers={"Authorization": "Bearer user"}).json() == []


def test_same_request_still_reuses_job_after_implementation_update(service, bundle, monkeypatch):
    client, policy, _ = lifecycle_fixture(service, bundle)
    assert (
        client.post(
            "/api/v1/compute/model-lifecycle-policies",
            headers={"Authorization": "Bearer operator"},
            json=policy.model_dump(mode="json"),
        ).status_code
        == 200
    )
    headers = {"Authorization": "Bearer user", "Idempotency-Key": "replay"}
    first = client.post(
        "/api/v1/compute/model-lifecycles", headers=headers, json={"policy_ref": policy.ref}
    ).json()
    original_read = Path.read_bytes

    def updated_source(path):
        original = original_read(path)
        return (
            original + b"\n# new loader revision\n" if path.name == "linear_model.py" else original
        )

    monkeypatch.setattr(Path, "read_bytes", updated_source)
    replay = client.post(
        "/api/v1/compute/model-lifecycles", headers=headers, json={"policy_ref": policy.ref}
    )
    assert replay.status_code == 200
    assert replay.json()["job_id"] == first["job_id"]
    fresh = client.post(
        "/api/v1/compute/model-lifecycles",
        headers={"Authorization": "Bearer user", "Idempotency-Key": "new-request"},
        json={"policy_ref": policy.ref},
    )
    assert fresh.status_code == 422


def test_concurrent_partial_start_replays_return_same_job(service, bundle, tmp_path):
    from resource_advisor.service import Service
    from resource_advisor.store import Store, outbox

    store = service.store
    owned_store = store.engine.dialect.name == "sqlite"
    if owned_store:
        # A real file provides two independent SQLite connections; StaticPool's
        # shared in-memory connection cannot reproduce a database uniqueness race.
        store = Store("sqlite:///" + str(tmp_path / "concurrent.db"))
        store.initialize()
        service = Service(store, accept_synthetic=True)
        spec, _, variant, capability = bundle
        for kind, value in [("workload", spec), ("variant", variant), ("capability", capability)]:
            service.register(kind, value, "team-a")
    client, policy, _ = lifecycle_fixture(service, bundle)
    assert (
        client.post(
            "/api/v1/compute/model-lifecycle-policies",
            headers={"Authorization": "Bearer operator"},
            json=policy.model_dump(mode="json"),
        ).status_code
        == 200
    )
    headers = {"Authorization": "Bearer user", "Idempotency-Key": "concurrent"}

    def start():
        response = client.post(
            "/api/v1/compute/model-lifecycles", headers=headers, json={"policy_ref": policy.ref}
        )
        assert response.status_code == 200, response.text
        return response.json()

    first = start()
    # Simulate a crash after compute acceptance, before lifecycle event enqueue.
    with store.transaction() as conn:
        conn.execute(delete(outbox).where(outbox.c.id == first["ref"]))
    barrier = Barrier(2)

    def synchronize(conn, cursor, statement, parameters, context, executemany):
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if statement.startswith("SELECT ra_outbox.id \n") and first["ref"] in values:
            barrier.wait(timeout=10)

    event.listen(store.engine, "after_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            replayed = list(pool.map(lambda _: start(), range(2)))
    finally:
        event.remove(store.engine, "after_cursor_execute", synchronize)
    assert [x["job_id"] for x in replayed] == [first["job_id"], first["job_id"]]
    assert len(client.get("/api/v1/compute/jobs", headers=headers).json()) == 1
    if owned_store:
        store.engine.dispose()
