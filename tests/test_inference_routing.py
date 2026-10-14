import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
from fastapi.testclient import TestClient

from resource_advisor.api import Principal, create_app

PREFIX = "/api/v1/compute/inference-pools"
MODEL = "resnet50-lab-v1"
SOURCE = "a" * 64


def router_client(service, gpu_artifact="b"):
    bindings = {}
    for ref, runtime, artifact in [("gpu", "cuda", gpu_artifact), ("npu", "hailo", "c")]:
        bindings[ref] = {
            "project_ref": "team-a",
            "url": f"http://{ref}.models.svc:8080",
            "model_ref": MODEL,
            "source_model_sha256": SOURCE,
            "runtime": runtime,
            "artifact_sha256": artifact * 64,
            "runtime_image_digest": "sha256:" + "d" * 64,
            "qualification_digest": "sha256:" + "e" * 64,
        }
    health = {"gpu": {"inflight": 3, "service_ms": 10}, "npu": {"inflight": 0, "service_ms": 15}}
    calls = []

    def identity(ref):
        return {
            key: value for key, value in bindings[ref].items() if key not in {"project_ref", "url"}
        } | {
            "input_contract": "rgb-f32-nhwc-224x224x3-v1",
            "output_contract": "imagenet-scores-f32-1000-v1",
        }

    def upstream(request):
        ref = request.url.host.split(".")[0]
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "ready": True,
                    "identity": identity(ref),
                    "max_inflight": 8,
                    "inflight": health[ref]["inflight"],
                    "service_ms": health[ref]["service_ms"],
                },
            )
        calls.append(ref)
        if health[ref].get("started"):
            health[ref]["started"].set()
            assert health[ref]["release"].wait(5)
        if health[ref].get("delay"):
            time.sleep(health[ref]["delay"])
        if health[ref].get("fail"):
            raise httpx.ReadTimeout("upstream unavailable", request=request)
        if health[ref].get("wrong_identity"):
            return httpx.Response(
                200, json={"identity": {"model_ref": "wrong"}, "predictions": [[1.0] + [0.0] * 999]}
            )
        count = len(json.loads(request.content)["instances"])
        return httpx.Response(
            200, json={"identity": identity(ref), "predictions": [[1.0] + [0.0] * 999] * count}
        )

    client = TestClient(
        create_app(
            service,
            {
                hashlib.sha256(name.encode()).hexdigest(): principal
                for name, principal in [
                    ("operator", Principal("team-a", True)),
                    ("user", Principal("team-a")),
                    ("other", Principal("team-b")),
                ]
            },
            inference_config={"endpoints": bindings},
            inference_client=httpx.Client(transport=httpx.MockTransport(upstream)),
        )
    )
    return client, health, calls


def test_project_scoped_pool_selects_ready_npu_over_queued_gpu(service):
    client, _, calls = router_client(service)
    body = {
        "ref": "images",
        "model_ref": MODEL,
        "source_model_sha256": SOURCE,
        "endpoint_refs": ["gpu", "npu"],
    }
    user = {"Authorization": "Bearer user"}
    assert client.post(PREFIX, headers=user, json=body).status_code == 403
    created = client.post(PREFIX, headers={"Authorization": "Bearer operator"}, json=body)
    assert created.status_code == 200, created.text
    assert (
        client.get(PREFIX + "/images", headers={"Authorization": "Bearer other"}).status_code == 404
    )
    response = client.post(
        PREFIX + "/images:predict", headers=user, json={"instances": [[0.0] * (224 * 224 * 3)]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["routing"]["endpoint_ref"] == "npu"
    assert response.json()["predictions"][0][0] == 1.0
    assert calls == ["npu"]


def test_failed_gpu_falls_back_without_leaking_or_retaining_reservation(service):
    client, health, calls = router_client(service)
    health["gpu"]["inflight"] = 0
    health["gpu"]["fail"] = True
    headers = {"Authorization": "Bearer operator"}
    assert (
        client.post(
            PREFIX,
            headers=headers,
            json={
                "ref": "images",
                "model_ref": MODEL,
                "source_model_sha256": SOURCE,
                "endpoint_refs": ["gpu", "npu"],
            },
        ).status_code
        == 200
    )
    response = client.post(
        PREFIX + "/images:predict",
        headers={"Authorization": "Bearer user"},
        json={"instances": [[0.0] * (224 * 224 * 3)]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["routing"]["fallback"]
    assert calls == ["gpu", "npu"]


def test_simultaneous_requests_reserve_gpu_and_choose_npu_for_next_request(service):
    client, health, calls = router_client(service)
    started, release = Event(), Event()
    health["gpu"].update(inflight=0, started=started, release=release)
    headers = {"Authorization": "Bearer operator"}
    assert (
        client.post(
            PREFIX,
            headers=headers,
            json={
                "ref": "images",
                "model_ref": MODEL,
                "source_model_sha256": SOURCE,
                "endpoint_refs": ["gpu", "npu"],
            },
        ).status_code
        == 200
    )
    headers = {"Authorization": "Bearer user"}
    payload = {"instances": [[0.0] * 150528]}
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            client.post, PREFIX + "/images:predict", headers=headers, json=payload
        )
        try:
            assert started.wait(5)
            second = client.post(PREFIX + "/images:predict", headers=headers, json=payload)
            assert second.status_code == 200
            assert second.json()["routing"]["endpoint_ref"] == "npu"
        finally:
            release.set()
        assert first.result(5).json()["routing"]["endpoint_ref"] == "gpu"
    assert calls == ["gpu", "npu"]


def test_late_valid_prediction_is_not_a_successful_response(service):
    client, health, _ = router_client(service)
    health["gpu"].update(inflight=0, delay=0.1)
    headers = {"Authorization": "Bearer operator"}
    assert (
        client.post(
            PREFIX,
            headers=headers,
            json={
                "ref": "images",
                "model_ref": MODEL,
                "source_model_sha256": SOURCE,
                "endpoint_refs": ["gpu", "npu"],
            },
        ).status_code
        == 200
    )
    response = client.post(
        PREFIX + "/images:predict",
        headers={"Authorization": "Bearer user"},
        json={"instances": [[0.0] * 150528], "deadline_ms": 50},
    )
    assert response.status_code == 503
    assert all(
        e["local_inflight"] == 0
        for e in client.get(PREFIX + "/images", headers=headers).json()["endpoints"]
    )
    state = client.get(PREFIX + "/images", headers=headers).json()
    assert all(endpoint["local_inflight"] == 0 for endpoint in state["endpoints"])
    assert state["endpoints"][0]["failures"] == 1


def test_wrong_model_response_is_not_returned_and_pool_binding_is_immutable(service):
    client, health, calls = router_client(service)
    health["gpu"].update(inflight=0, wrong_identity=True)
    headers = {"Authorization": "Bearer operator"}
    body = {
        "ref": "images",
        "model_ref": MODEL,
        "source_model_sha256": SOURCE,
        "endpoint_refs": ["gpu", "npu"],
    }
    assert client.post(PREFIX, headers=headers, json=body).status_code == 200
    assert (
        client.post(PREFIX, headers=headers, json={**body, "endpoint_refs": ["gpu"]}).status_code
        == 409
    )
    response = client.post(
        PREFIX + "/images:predict",
        headers={"Authorization": "Bearer user"},
        json={"instances": [[0.0] * (224 * 224 * 3)]},
    )
    assert response.status_code == 200
    assert response.json()["routing"]["attempts"][0]["outcome"] == "IDENTITY_OR_OUTPUT_MISMATCH"
    assert calls == ["gpu", "npu"]


def test_pool_catalog_is_project_scoped_and_marks_changed_bindings(service):
    client, _, _ = router_client(service)
    value = {
        "ref": "images",
        "model_ref": MODEL,
        "source_model_sha256": SOURCE,
        "endpoint_refs": ["gpu", "npu"],
    }
    assert (
        client.post(PREFIX, headers={"Authorization": "Bearer operator"}, json=value).status_code
        == 200
    )
    user = client.get(PREFIX, headers={"Authorization": "Bearer user"})
    assert user.status_code == 200
    body = user.json()
    assert body["items"][0]["ref"] == "images"
    assert body["items"][0]["configured"] is True
    assert body["items"][0]["runtimes"] == ["cuda", "hailo"]
    assert "url" not in json.dumps(body)
    assert client.get(PREFIX, headers={"Authorization": "Bearer other"}).json()["items"] == []
    assert client.get(PREFIX).status_code == 401
    assert (
        client.get(PREFIX + "?page=1", headers={"Authorization": "Bearer user"}).json()["items"]
        == []
    )
    changed, _, _ = router_client(service, gpu_artifact="f")
    assert (
        changed.get(PREFIX, headers={"Authorization": "Bearer user"}).json()["items"][0][
            "configured"
        ]
        is False
    )
