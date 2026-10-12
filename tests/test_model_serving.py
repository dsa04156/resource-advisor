import base64
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_model_lifecycle import (
    RegistryServer,
    finish_training,
    lifecycle_fixture,
    registry_delivery,
)

from resource_advisor.api import Principal, create_app


def registered_fixture(service, bundle):
    client, policy, binding = lifecycle_fixture(service, bundle)
    started, storage = finish_training(service, client, policy, binding)
    server = RegistryServer()
    delivery = registry_delivery(service, storage, server)
    assert delivery.deliver_one()
    return client, started["ref"], delivery.registry, server


def deploy(client, lifecycle, *, key="deploy-one", expected=None):
    return client.post(
        "/api/v1/compute/model-deployments",
        headers={"Authorization": "Bearer operator", "Idempotency-Key": key},
        json={
            "service_ref": "linear",
            "lifecycle_ref": lifecycle,
            "expected_deployment_ref": expected,
        },
    )


def test_operator_deployment_is_project_owned_and_idempotent(service, bundle):
    client, ref, _, _ = registered_fixture(service, bundle)
    first = deploy(client, ref)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "PENDING"
    assert deploy(client, ref).json()["ref"] == first.json()["ref"]
    headers = {"Authorization": "Bearer user"}
    view = client.get("/api/v1/compute/model-services/linear", headers=headers).json()
    assert view["desired"]["ref"] == first.json()["ref"]
    assert len(view["history"]) == 1
    assert (
        client.get(
            "/api/v1/compute/model-deployments/" + first.json()["ref"],
            headers={"Authorization": "Bearer other"},
        ).status_code
        == 404
    )
    denied = client.post(
        "/api/v1/compute/model-deployments",
        headers={**headers, "Idempotency-Key": "user-deploy"},
        json={"service_ref": "linear", "lifecycle_ref": ref},
    )
    assert denied.status_code == 403
    assert deploy(client, ref, key="stale-new-command").status_code == 409


def prediction_client(service, upstream):
    return TestClient(
        create_app(
            service,
            {hashlib.sha256(b"user").hexdigest(): Principal("team-a")},
            prediction_client=upstream,
        )
    )


def package_fixture(registry, server):
    registration = server.versions[0]
    artifact_path = registration["source"].split("/", 2)[2]
    files = {
        name.split("/" + artifact_path + "/", 1)[1]: base64.b64encode(data).decode()
        for name, data in server.files.items()
        if "/" + artifact_path + "/" in name
    }
    tags = {t["key"]: t["value"] for t in registration["tags"]}
    return {
        "files": files,
        "description": {
            "service_name": "ra-model-test",
            "deployment_ref": "mdp-test",
            "model_uri": "models:/ra-team-a-linear/1",
            "model_package_digest": tags["resource_advisor.package_digest"],
            "output_checkpoint_digest": tags["resource_advisor.output_checkpoint_digest"],
        },
    }


def test_predictor_loads_verified_registered_checkpoint_and_rejects_tampering(service, bundle):
    from resource_advisor.model_predictor import create_predictor

    _, _, registry, server = registered_fixture(service, bundle)
    package = package_fixture(registry, server)
    predictor = TestClient(create_predictor(package))
    response = predictor.post(
        "/v1/models/ra-model-test:predict", json={"instances": [[0.2, 0.3], [0.6, 0.1]]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["predictions"][0][0] == pytest.approx(0.25)
    assert response.json()["predictions"][1][0] == pytest.approx(1.65)
    assert response.json()["model"]["model_uri"] == "models:/ra-team-a-linear/1"
    assert (
        predictor.post("/v1/models/ra-model-test:predict", json={"instances": [[1.0]]}).status_code
        == 422
    )
    package["files"]["data/checkpoint.json"] = base64.b64encode(b'{"weights": [[0],[0]]}').decode()
    with pytest.raises(ValueError, match="integrity"):
        create_predictor(package)


class KubeServer:
    """Kubernetes/HTTP boundary double; real fixed predictor handles its requests."""

    def __init__(self):
        self.objects = {}
        self.rolled_out = False
        self.resource_version = 0
        self.lose_response = False

    def handle(self, request):
        path = request.url.path
        if request.method in ("POST", "PUT"):
            obj = json.loads(request.content)
            name = obj["metadata"]["name"]
            key = path + "/" + name if request.method == "POST" else path
            if request.method == "POST" and key in self.objects:
                return httpx.Response(409)
            old = self.objects.get(key)
            if (
                old
                and request.method == "PUT"
                and obj["metadata"].get("resourceVersion") != old["metadata"]["resourceVersion"]
            ):
                return httpx.Response(409)
            self.resource_version += 1
            obj["metadata"].update(
                uid=old["metadata"]["uid"] if old else "uid-" + name,
                resourceVersion=str(self.resource_version),
                generation=self.resource_version,
            )
            self.objects[key] = obj
            if self.lose_response and obj["kind"] == "InferenceService":
                self.lose_response = False
                raise httpx.ReadTimeout("response lost after accepted write", request=request)
            return httpx.Response(201 if request.method == "POST" else 200, json=obj)
        if path in self.objects:
            return httpx.Response(200, json=self.objects[path])
        isvc = next((x for x in self.objects.values() if x["kind"] == "InferenceService"), None)
        if isvc and path.endswith("/" + isvc["metadata"]["name"] + "-predictor"):
            owner = [
                {"uid": isvc["metadata"]["uid"], "controller": True, "kind": "InferenceService"}
            ]
            if "/deployments/" in path:
                obj = {
                    "metadata": {
                        "uid": "deployment-uid",
                        "generation": 10,
                        "ownerReferences": owner,
                    },
                    "spec": {"replicas": 1, "template": {"spec": isvc["spec"]["predictor"]}},
                    "status": {
                        "observedGeneration": 10 if self.rolled_out else 9,
                        "replicas": 1,
                        "readyReplicas": 1,
                        "updatedReplicas": 1,
                        "availableReplicas": 1,
                    },
                }
            else:
                obj = {
                    "metadata": {"uid": "service-uid", "ownerReferences": owner},
                    "spec": {"type": "ClusterIP", "ports": [{"port": 80, "targetPort": 8080}]},
                }
            return httpx.Response(200, json=obj)
        return httpx.Response(404, json={"reason": "NotFound"})

    def predict(self, request):
        from resource_advisor.model_predictor import create_predictor

        isvc = next(x for x in self.objects.values() if x["kind"] == "InferenceService")
        cm_name = isvc["spec"]["predictor"]["volumes"][0]["configMap"]["name"]
        cm = next(
            x
            for x in self.objects.values()
            if x["kind"] == "ConfigMap" and x["metadata"]["name"] == cm_name
        )
        predictor = TestClient(create_predictor(json.loads(cm["data"]["package.json"])))
        response = predictor.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200, (request.method, request.url.path, response.text)
        return httpx.Response(response.status_code, json=response.json())


def serving_delivery(service, registry, kube):
    from resource_advisor.kserve_deployment import KServeAdapter, ServingConfig
    from resource_advisor.serving_delivery import ServingDelivery

    config = ServingConfig(
        project_namespaces={"team-a": "model-lab"},
        image="example.invalid/predictor@sha256:" + "a" * 64,
        node_selector={"kubernetes.io/hostname": "cpu-node"},
    )
    adapter = KServeAdapter(
        config,
        client=httpx.Client(
            base_url="https://kube.invalid", transport=httpx.MockTransport(kube.handle)
        ),
        prediction_client=httpx.Client(transport=httpx.MockTransport(kube.predict)),
    )
    return ServingDelivery(service.store, registry, adapter, job_routes={("team-a", "lab")})


def test_only_actual_rollout_and_predictions_make_deployment_ready(service, bundle, monkeypatch):
    from datetime import timedelta

    from resource_advisor.contracts import now

    client, ref, registry, _ = registered_fixture(service, bundle)
    view = deploy(client, ref).json()
    kube = KubeServer()
    delivery = serving_delivery(service, registry, kube)
    assert delivery.deliver_one()
    first = client.get(
        "/api/v1/compute/model-deployments/" + view["ref"], headers={"Authorization": "Bearer user"}
    ).json()
    assert first["state"] == "PENDING"
    assert first["delivery_error"] == "ROLLOUT_NOT_COMPLETE"
    advanced = now() + timedelta(seconds=400)
    monkeypatch.setattr("resource_advisor.store.now", lambda: advanced)
    kube.rolled_out = True
    assert delivery.deliver_one()
    ready = client.get(
        "/api/v1/compute/model-deployments/" + view["ref"], headers={"Authorization": "Bearer user"}
    ).json()
    assert ready["state"] == "READY", ready["delivery_error"]
    assert ready["receipt"]["model_uri"] == "models:/ra-team-a-linear/1"
    assert ready["receipt"]["prediction_count"] == 2
    api = prediction_client(service, httpx.Client(transport=httpx.MockTransport(kube.predict)))
    prediction = api.post(
        "/api/v1/compute/model-services/linear:predict",
        headers={"Authorization": "Bearer user"},
        json={"instances": [[0.2, 0.3]]},
    )
    assert prediction.status_code == 200, prediction.text
    assert prediction.json()["predictions"][0][0] == pytest.approx(0.25)


def test_rollback_reuses_verified_model_and_response_loss_keeps_one_service(
    service, bundle, monkeypatch
):
    from datetime import timedelta

    from resource_advisor.contracts import now

    client, ref, registry, _ = registered_fixture(service, bundle)
    kube = KubeServer()
    kube.rolled_out = True
    kube.lose_response = True
    runner = serving_delivery(service, registry, kube)
    first = deploy(client, ref).json()
    assert runner.deliver_one()
    assert (
        client.get(
            "/api/v1/compute/model-deployments/" + first["ref"],
            headers={"Authorization": "Bearer user"},
        ).json()["state"]
        == "PENDING"
    )
    advanced = now() + timedelta(seconds=400)
    monkeypatch.setattr("resource_advisor.store.now", lambda: advanced)
    assert runner.deliver_one()
    first = client.get(
        "/api/v1/compute/model-deployments/" + first["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    second = deploy(client, ref, key="deploy-two", expected=first["ref"]).json()
    assert runner.deliver_one()
    request = {
        "service_ref": "linear",
        "target_deployment_ref": first["ref"],
        "expected_deployment_ref": second["ref"],
    }
    headers = {"Authorization": "Bearer operator", "Idempotency-Key": "rollback-one"}
    rollback = client.post("/api/v1/compute/model-rollbacks", headers=headers, json=request)
    assert rollback.status_code == 200, rollback.text
    assert runner.deliver_one()
    restored = client.get(
        "/api/v1/compute/model-deployments/" + rollback.json()["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    assert restored["state"] == "READY", restored["delivery_error"]
    assert restored["receipt"]["isvc_uid"] == first["receipt"]["isvc_uid"]
    assert restored["receipt"]["model_uri"] == first["receipt"]["model_uri"]
    assert (
        client.post("/api/v1/compute/model-rollbacks", headers=headers, json=request).json()["ref"]
        == restored["ref"]
    )
    history = client.get(
        "/api/v1/compute/model-services/linear", headers={"Authorization": "Bearer user"}
    ).json()
    assert len(history["history"]) == 3
    assert len([x for x in kube.objects.values() if x["kind"] == "InferenceService"]) == 1


def test_corrupted_registry_package_never_creates_inference_service(service, bundle):
    client, ref, registry, server = registered_fixture(service, bundle)
    view = deploy(client, ref).json()
    key = next(k for k in server.files if k.endswith("data/checkpoint.json"))
    server.files[key] = b"{}"
    kube = KubeServer()
    assert serving_delivery(service, registry, kube).deliver_one()
    result = client.get(
        "/api/v1/compute/model-deployments/" + view["ref"], headers={"Authorization": "Bearer user"}
    ).json()
    assert result["state"] == "PENDING"
    assert result["delivery_error"] == "MODEL_PACKAGE_INTEGRITY_MISMATCH"
    assert not kube.objects


def test_quality_rejection_cannot_be_selected_for_serving(service, bundle):
    client, policy, binding = lifecycle_fixture(service, bundle, maximum_mse=0.01)
    started, storage = finish_training(service, client, policy, binding)
    from resource_advisor.model_lifecycle import LifecycleDelivery

    assert LifecycleDelivery(service, storage, None).deliver_one()
    assert deploy(client, started["ref"]).status_code == 422


def test_foreign_native_service_is_never_overwritten(service, bundle):
    from resource_advisor.contracts import signature

    client, ref, registry, _ = registered_fixture(service, bundle)
    view = deploy(client, ref).json()
    kube = KubeServer()
    name = "ra-model-" + signature(["team-a", "linear"])[7:39]
    original = {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {"name": name, "uid": "foreign", "labels": {"owner": "someone-else"}},
        "spec": {"predictor": {"model": {"modelFormat": {"name": "sklearn"}}}},
    }
    kube.objects[
        "/apis/serving.kserve.io/v1beta1/namespaces/model-lab/inferenceservices/" + name
    ] = original
    assert serving_delivery(service, registry, kube).deliver_one()
    result = client.get(
        "/api/v1/compute/model-deployments/" + view["ref"], headers={"Authorization": "Bearer user"}
    ).json()
    assert result["delivery_error"] == "FOREIGN_SERVING_RESOURCE"
    assert original["metadata"]["uid"] == "foreign"
    assert original["spec"]["predictor"]["model"]["modelFormat"]["name"] == "sklearn"


def test_superseded_command_cannot_replace_newer_target(service, bundle):
    client, ref, registry, _ = registered_fixture(service, bundle)
    first = deploy(client, ref).json()
    second = deploy(client, ref, key="newer", expected=first["ref"]).json()
    kube = KubeServer()
    kube.rolled_out = True
    runner = serving_delivery(service, registry, kube)
    assert runner.deliver_one()
    assert runner.deliver_one()
    first = client.get(
        "/api/v1/compute/model-deployments/" + first["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    second = client.get(
        "/api/v1/compute/model-deployments/" + second["ref"],
        headers={"Authorization": "Bearer user"},
    ).json()
    assert first["state"] == "SUPERSEDED"
    assert second["state"] == "READY"
    denied = client.post(
        "/api/v1/compute/model-rollbacks",
        headers={"Authorization": "Bearer operator", "Idempotency-Key": "unverified-target"},
        json={
            "service_ref": "linear",
            "target_deployment_ref": first["ref"],
            "expected_deployment_ref": second["ref"],
        },
    )
    assert denied.status_code == 422


def test_concurrent_commands_accept_only_one_successor(service, bundle, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from resource_advisor.service import Service
    from resource_advisor.store import Store

    if service.store.engine.dialect.name == "sqlite":
        store = Store("sqlite:///" + str(tmp_path / "serving.db"))
        store.initialize()
        service = Service(store)
        spec, _, variant, capability = bundle
        for kind, model in [("workload", spec), ("variant", variant), ("capability", capability)]:
            service.register(kind, model, "team-a")
    client, ref, _, _ = registered_fixture(service, bundle)
    barrier = Barrier(2)

    def start(key):
        barrier.wait(timeout=10)
        return deploy(client, ref, key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(start, ["first-writer", "second-writer"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    history = client.get(
        "/api/v1/compute/model-services/linear", headers={"Authorization": "Bearer user"}
    ).json()
    assert len(history["history"]) == 1


def test_simultaneous_retry_returns_same_deployment(service, bundle, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from resource_advisor.service import Service
    from resource_advisor.store import Store

    if service.store.engine.dialect.name == "sqlite":
        store = Store("sqlite:///" + str(tmp_path / "retry.db"))
        store.initialize()
        service = Service(store)
        spec, _, variant, capability = bundle
        for kind, model in [("workload", spec), ("variant", variant), ("capability", capability)]:
            service.register(kind, model, "team-a")
    client, ref, _, _ = registered_fixture(service, bundle)
    paused, resume = Event(), Event()
    original = service.store.get

    def interleaved(conn, kind, entity_ref):
        result = original(conn, kind, entity_ref)
        if kind == "model_deployment" and result is None and not paused.is_set():
            paused.set()
            assert resume.wait(timeout=10)
        return result

    monkeypatch.setattr(service.store, "get", interleaved)
    with ThreadPoolExecutor(max_workers=1) as pool:
        slow = pool.submit(deploy, client, ref)
        assert paused.wait(timeout=10)
        fast = deploy(client, ref)
        resume.set()
        retry = slow.result(timeout=10)
    assert fast.status_code == retry.status_code == 200, (fast.text, retry.text)
    assert fast.json()["ref"] == retry.json()["ref"]
    assert (
        len(
            client.get(
                "/api/v1/compute/model-services/linear", headers={"Authorization": "Bearer user"}
            ).json()["history"]
        )
        == 1
    )


@pytest.mark.parametrize(
    "tamper", ["account", "token", "privilege", "command", "mount", "credentials", "seccomp"]
)
def test_changed_native_runtime_contract_never_becomes_ready(service, bundle, tamper):
    client, ref, registry, _ = registered_fixture(service, bundle)
    view = deploy(client, ref).json()
    kube = KubeServer()
    kube.rolled_out = True
    original = kube.handle

    def altered(request):
        response = original(request)
        if (
            request.method == "GET"
            and "/deployments/" in request.url.path
            and response.status_code == 200
        ):
            obj = response.json()
            pod = obj["spec"]["template"]["spec"]
            container = pod["containers"][0]
            if tamper == "account":
                pod["serviceAccountName"] = "ra-worker"
            elif tamper == "token":
                pod["automountServiceAccountToken"] = True
            elif tamper == "privilege":
                container["securityContext"]["allowPrivilegeEscalation"] = True
            elif tamper == "seccomp":
                container["securityContext"]["seccompProfile"] = {"type": "Unconfined"}
            elif tamper == "command":
                container["command"] = ["python", "-m", "unexpected.module"]
            elif tamper == "mount":
                container["volumeMounts"][0]["readOnly"] = False
            else:
                container["env"].append(
                    {
                        "name": "CREDENTIAL",
                        "valueFrom": {"secretKeyRef": {"name": "private", "key": "token"}},
                    }
                )
            return httpx.Response(200, json=obj)
        return response

    kube.handle = altered
    assert serving_delivery(service, registry, kube).deliver_one()
    result = client.get(
        "/api/v1/compute/model-deployments/" + view["ref"], headers={"Authorization": "Bearer user"}
    ).json()
    assert result["state"] == "PENDING"
    assert result["delivery_error"] == "PREDICTOR_TEMPLATE_MISMATCH"


def test_admission_cannot_replace_operator_runtime_contract(service, bundle):
    client, ref, registry, _ = registered_fixture(service, bundle)
    view = deploy(client, ref).json()
    kube = KubeServer()
    kube.rolled_out = True
    original = kube.handle

    def changed_admission(request):
        response = original(request)
        if request.method == "POST" and "/inferenceservices" in request.url.path:
            obj = kube.objects[request.url.path + "/" + response.json()["metadata"]["name"]]
            predictor = obj["spec"]["predictor"]
            predictor["serviceAccountName"] = "ra-worker"
            predictor["automountServiceAccountToken"] = True
            predictor["containers"][0]["image"] = "unexpected/predictor@sha256:" + "b" * 64
            return httpx.Response(201, json=obj)
        return response

    kube.handle = changed_admission
    assert serving_delivery(service, registry, kube).deliver_one()
    result = client.get(
        "/api/v1/compute/model-deployments/" + view["ref"], headers={"Authorization": "Bearer user"}
    ).json()
    assert result["state"] == "PENDING"
    assert result["delivery_error"] == "PREDICTOR_TEMPLATE_MISMATCH"
