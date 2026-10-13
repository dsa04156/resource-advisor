import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient
from test_model_serving import KubeServer, registered_fixture, serving_delivery

from resource_advisor.api import Principal, create_app

PREFIX = "/api/v1/compute"


def release_client(service, upstream=None):
    return TestClient(
        create_app(
            service,
            {
                hashlib.sha256(b"operator").hexdigest(): Principal(
                    "team-a", True, "release-reviewer"
                ),
                hashlib.sha256(b"user").hexdigest(): Principal("team-a"),
                hashlib.sha256(b"other").hexdigest(): Principal("team-b", True),
            },
            prediction_client=upstream,
        )
    )


def api(client, method, path, *, who="user", key=None, **kwargs):
    headers = {"Authorization": "Bearer " + who}
    if key:
        headers["Idempotency-Key"] = key
    return client.request(method, PREFIX + path, headers=headers, **kwargs)


def policy_request(lifecycle, *, baseline=None):
    return {
        "ref": "release-policy-one",
        "project_ref": "team-a",
        "lifecycle_policy_ref": lifecycle["policy_ref"],
        "production_service_ref": "linear",
        "expected_production_deployment_ref": baseline,
        "maximum_mse_regression": 0.0,
        "maximum_probe_latency_ms": 10000,
        "verification_ttl_seconds": 3600,
    }


def test_candidate_is_frozen_project_owned_and_does_not_deploy(service, bundle):
    _, lifecycle_ref, _, _ = registered_fixture(service, bundle)
    client = release_client(service)
    lifecycle = api(client, "GET", "/model-lifecycles/" + lifecycle_ref).json()
    policy = policy_request(lifecycle)
    registered = api(client, "POST", "/model-release-policies", who="operator", json=policy)
    assert registered.status_code == 200, registered.text
    request = {"policy_ref": policy["ref"], "lifecycle_ref": lifecycle_ref}
    candidate = api(client, "POST", "/model-releases", key="candidate-one", json=request)
    assert candidate.status_code == 200, candidate.text
    view = candidate.json()
    assert view["state"] == "CANDIDATE"
    assert view["registration"] == lifecycle["registration"]
    assert view["comparison"]["baseline_mse"] is None
    assert api(client, "GET", "/model-services/linear").status_code == 404
    assert api(client, "POST", "/model-releases", key="candidate-one", json=request).json() == view
    assert api(client, "GET", "/model-releases/" + view["ref"], who="other").status_code == 404
    assert api(client, "POST", "/model-release-policies", json=policy).status_code == 403
    assert api(client, "GET", "/model-releases").json()["items"][0]["ref"] == view["ref"]
    changed = {**policy, "maximum_mse_regression": 1.0}
    assert (
        api(client, "POST", "/model-release-policies", who="operator", json=changed).status_code
        == 409
    )


def staged_fixture(service, bundle):
    _, lifecycle_ref, registry, _ = registered_fixture(service, bundle)
    kube = KubeServer()
    kube.rolled_out = True
    client = release_client(service, httpx.Client(transport=httpx.MockTransport(kube.predict)))
    lifecycle = api(client, "GET", "/model-lifecycles/" + lifecycle_ref).json()
    policy = policy_request(lifecycle)
    response = api(client, "POST", "/model-release-policies", who="operator", json=policy)
    assert response.status_code == 200, response.text
    response = api(
        client,
        "POST",
        "/model-releases",
        key="candidate-one",
        json={"policy_ref": policy["ref"], "lifecycle_ref": lifecycle_ref},
    )
    assert response.status_code == 200, response.text
    return client, response.json(), serving_delivery(service, registry, kube), kube


def test_project_user_stages_and_freezes_verified_probes_without_production_change(service, bundle):
    client, release, runner, _ = staged_fixture(service, bundle)
    path = "/model-releases/" + release["ref"]
    staged = api(client, "POST", path + "/staging")
    assert staged.status_code == 200, staged.text
    assert staged.json()["staging"]["service_ref"] == release["staging_service_ref"]
    assert api(client, "POST", path + "/verification").status_code == 503
    assert runner.deliver_one()
    verified = api(client, "POST", path + "/verification")
    assert verified.status_code == 200, verified.text
    receipt = verified.json()["verification"]
    assert verified.json()["state"] == "PENDING_APPROVAL"
    assert receipt["model_uri"] == release["registration"]["model_uri"]
    assert len(receipt["probes"]) == 3
    assert all(
        x["prediction_count"] == 2 and x["maximum_abs_error"] <= 1e-12 for x in receipt["probes"]
    )
    assert api(client, "POST", path + "/verification").json()["verification"] == receipt
    assert (
        api(client, "POST", path + "/staging").json()["staging"]["ref"]
        == staged.json()["staging"]["ref"]
    )
    assert api(client, "POST", path + "/staging", who="other").status_code == 404
    assert api(client, "GET", "/model-services/linear").status_code == 404


def test_only_operator_decides_and_production_requires_exact_approval(service, bundle):
    from resource_advisor.contracts import signature

    client, release, runner, kube = staged_fixture(service, bundle)
    path = "/model-releases/" + release["ref"]
    bypass = api(
        client,
        "POST",
        "/model-deployments",
        who="operator",
        key="bypass",
        json={"service_ref": "linear", "lifecycle_ref": release["request"]["lifecycle_ref"]},
    )
    assert bypass.status_code == 422, bypass.text
    assert api(client, "POST", path + "/deployment", who="operator").status_code == 422
    assert api(client, "POST", path + "/staging").status_code == 200
    assert runner.deliver_one()
    verified = api(client, "POST", path + "/verification").json()
    decision = {
        "decision": "approve",
        "verification_digest": signature(verified["verification"]),
        "reason": "Reviewed quality comparison and three staging probes",
    }
    assert api(client, "POST", path + "/decisions", json=decision).status_code == 403
    approved = api(client, "POST", path + "/decisions", who="operator", json=decision)
    assert approved.status_code == 200, approved.text
    assert approved.json()["decision"]["actor"] == "release-reviewer"
    assert approved.json()["state"] == "APPROVED"
    assert (
        api(client, "POST", path + "/decisions", who="operator", json=decision).json()
        == approved.json()
    )
    assert api(client, "GET", "/model-services/linear").status_code == 404
    # A separate native HTTP double handles the production resource; evidence
    # in the real API/store remains unchanged and no internals are mocked.
    production_kube = KubeServer()
    production_kube.rolled_out = True
    production_runner = serving_delivery(service, runner.registry, production_kube)
    deployed = api(client, "POST", path + "/deployment", who="operator")
    assert deployed.status_code == 200, deployed.text
    assert production_runner.deliver_one()
    finished = api(client, "GET", path).json()
    assert finished["state"] == "DEPLOYED"
    assert finished["deployment"]["receipt"]["model_uri"] == release["registration"]["model_uri"]
    assert finished["deployment"]["release_ref"] == release["ref"]
    assert api(client, "POST", path + "/deployment", who="operator").json() == finished
    assert len(api(client, "GET", "/model-services/linear").json()["history"]) == 1


def verified_fixture(service, bundle):
    client, release, runner, kube = staged_fixture(service, bundle)
    path = "/model-releases/" + release["ref"]
    assert api(client, "POST", path + "/staging").status_code == 200
    assert runner.deliver_one()
    verified = api(client, "POST", path + "/verification")
    assert verified.status_code == 200, verified.text
    return client, verified.json(), runner, kube


def decision_request(release, decision="approve"):
    from resource_advisor.contracts import signature

    return {
        "decision": decision,
        "verification_digest": signature(release["verification"]),
        "reason": "Acceptance review of frozen staging evidence",
    }


def test_rejected_decision_cannot_be_rewritten_or_deployed(service, bundle):
    client, release, _, _ = verified_fixture(service, bundle)
    path = "/model-releases/" + release["ref"]
    rejected = api(
        client,
        "POST",
        path + "/decisions",
        who="operator",
        json=decision_request(release, "reject"),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "REJECTED"
    assert (
        api(
            client, "POST", path + "/decisions", who="operator", json=decision_request(release)
        ).status_code
        == 409
    )
    assert api(client, "POST", path + "/deployment", who="operator").status_code == 422
    assert api(client, "GET", "/model-services/linear").status_code == 404


def test_expired_verification_cannot_be_approved_or_promoted(service, bundle, monkeypatch):
    from datetime import datetime, timedelta

    client, release, _, _ = verified_fixture(service, bundle)
    path = "/model-releases/" + release["ref"]
    assert (
        api(
            client, "POST", path + "/decisions", who="operator", json=decision_request(release)
        ).status_code
        == 200
    )
    expired = datetime.fromisoformat(release["verification"]["verified_at"]) + timedelta(
        seconds=3601
    )
    monkeypatch.setattr("resource_advisor.model_release.now", lambda: expired)
    result = api(client, "POST", path + "/deployment", who="operator")
    assert result.status_code == 422, result.text
    assert "expired" in api(client, "GET", path).json()["promotion_blocker"]
    assert api(client, "GET", "/model-services/linear").status_code == 404


@pytest.mark.parametrize("failure", ["prediction", "latency", "identity"])
def test_bad_live_probes_never_create_approval_evidence(service, bundle, failure):
    client, release, runner, kube = staged_fixture(service, bundle)
    path = "/model-releases/" + release["ref"]
    assert api(client, "POST", path + "/staging").status_code == 200
    assert runner.deliver_one()

    def bad_prediction(request):
        if failure == "latency":
            import time

            time.sleep(0.02)
        response = kube.predict(request)
        output = response.json()
        if failure == "prediction":
            output["predictions"][0][0] = 100.0
        if failure == "identity":
            output["model"]["model_uri"] = "models:/ra-team-a-linear/999"
        return httpx.Response(200, json=output)

    altered = release_client(service, httpx.Client(transport=httpx.MockTransport(bad_prediction)))
    if failure == "latency":
        # New policy frozen before its candidate and probes, with an intentionally
        # strict ceiling; the accepted policy is never edited after observations.
        lifecycle = api(
            client, "GET", "/model-lifecycles/" + release["request"]["lifecycle_ref"]
        ).json()
        policy = {
            **policy_request(lifecycle),
            "ref": "strict-latency",
            "maximum_probe_latency_ms": 1,
        }
        assert (
            api(client, "POST", "/model-release-policies", who="operator", json=policy).status_code
            == 200
        )
        release = api(
            client,
            "POST",
            "/model-releases",
            key="strict-latency-candidate",
            json={**release["request"], "policy_ref": policy["ref"]},
        ).json()
        path = "/model-releases/" + release["ref"]
        stage_kube = KubeServer()
        stage_kube.rolled_out = True
        assert api(client, "POST", path + "/staging").status_code == 200
        assert serving_delivery(service, runner.registry, stage_kube).deliver_one()
        kube = stage_kube
    failed = api(altered, "POST", path + "/verification")
    assert failed.status_code == (503 if failure == "identity" else 422), failed.text
    assert api(client, "GET", path).json()["verification"] is None


def test_decision_rejects_wrong_digest_missing_subject_and_client_actor(service, bundle):
    client, release, _, kube = verified_fixture(service, bundle)
    path = "/model-releases/" + release["ref"] + "/decisions"
    decision = decision_request(release)
    wrong = api(
        client,
        "POST",
        path,
        who="operator",
        json={**decision, "verification_digest": "sha256:" + "f" * 64},
    )
    assert wrong.status_code == 409, wrong.text
    assert (
        api(client, "POST", path, who="operator", json={**decision, "actor": "forged"}).status_code
        == 422
    )
    anonymous_operator = TestClient(
        create_app(service, {hashlib.sha256(b"operator").hexdigest(): Principal("team-a", True)})
    )
    assert api(anonymous_operator, "POST", path, who="operator", json=decision).status_code == 422


def test_emergency_staging_rollback_invalidates_existing_approval(service, bundle):
    client, release, _, _ = verified_fixture(service, bundle)
    path = "/model-releases/" + release["ref"]
    assert (
        api(
            client, "POST", path + "/decisions", who="operator", json=decision_request(release)
        ).status_code
        == 200
    )
    rollback = api(
        client,
        "POST",
        "/model-rollbacks",
        who="operator",
        key="stage-emergency",
        json={
            "service_ref": release["staging_service_ref"],
            "target_deployment_ref": release["staging"]["ref"],
            "expected_deployment_ref": release["staging"]["ref"],
        },
    )
    assert rollback.status_code == 200, rollback.text
    assert api(client, "POST", path + "/deployment", who="operator").status_code == 409
    assert api(client, "GET", "/model-services/linear").status_code == 404


def test_baseline_comparison_is_frozen_and_production_rollback_invalidates_promotion(
    service, bundle
):
    old_client, lifecycle_ref, registry, _ = registered_fixture(service, bundle)
    kube = KubeServer()
    kube.rolled_out = True
    runner = serving_delivery(service, registry, kube)
    from test_model_serving import deploy

    first = deploy(old_client, lifecycle_ref).json()
    assert runner.deliver_one()
    second = deploy(old_client, lifecycle_ref, key="second-baseline", expected=first["ref"]).json()
    assert runner.deliver_one()
    client = release_client(service)
    life = api(client, "GET", "/model-lifecycles/" + lifecycle_ref).json()
    policy = policy_request(life, baseline=second["ref"])
    assert (
        api(client, "POST", "/model-release-policies", who="operator", json=policy).status_code
        == 200
    )
    release = api(
        client,
        "POST",
        "/model-releases",
        key="baseline-candidate",
        json={"policy_ref": policy["ref"], "lifecycle_ref": lifecycle_ref},
    ).json()
    assert release["comparison"]["baseline_mse"] == pytest.approx(0.0625)
    assert release["comparison"]["candidate_mse"] == pytest.approx(0.0625)
    staging = KubeServer()
    staging.rolled_out = True
    client = release_client(service, httpx.Client(transport=httpx.MockTransport(staging.predict)))
    path = "/model-releases/" + release["ref"]
    assert api(client, "POST", path + "/staging").status_code == 200
    assert serving_delivery(service, registry, staging).deliver_one()
    release = api(client, "POST", path + "/verification").json()
    assert (
        api(
            client, "POST", path + "/decisions", who="operator", json=decision_request(release)
        ).status_code
        == 200
    )
    rollback = api(
        client,
        "POST",
        "/model-rollbacks",
        who="operator",
        key="production-emergency",
        json={
            "service_ref": "linear",
            "target_deployment_ref": first["ref"],
            "expected_deployment_ref": second["ref"],
        },
    )
    assert rollback.status_code == 200, rollback.text
    result = api(client, "POST", path + "/deployment", who="operator")
    assert result.status_code == 409, result.text
    history = api(client, "GET", "/model-services/linear").json()
    assert history["desired"]["ref"] == rollback.json()["ref"]
    assert len(history["history"]) == 3


@pytest.mark.parametrize("same_release", [True, False])
def test_concurrent_promotion_reconciles_retries_and_serializes_competitors(
    service, bundle, tmp_path, same_release
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from resource_advisor.service import Service
    from resource_advisor.store import Store

    if service.store.engine.dialect.name == "sqlite":
        store = Store("sqlite:///" + str(tmp_path / "promotion.db"))
        store.initialize()
        service = Service(store)
        spec, _, variant, capability = bundle
        for kind, model in [("workload", spec), ("variant", variant), ("capability", capability)]:
            service.register(kind, model, "team-a")
    client, release, runner, _ = verified_fixture(service, bundle)
    first = "/model-releases/" + release["ref"]
    assert (
        api(
            client, "POST", first + "/decisions", who="operator", json=decision_request(release)
        ).status_code
        == 200
    )
    second = first
    if not same_release:
        other = api(
            client, "POST", "/model-releases", key="competing-candidate", json=release["request"]
        ).json()
        second = "/model-releases/" + other["ref"]
        kube = KubeServer()
        kube.rolled_out = True
        alternate = release_client(
            service, httpx.Client(transport=httpx.MockTransport(kube.predict))
        )
        assert api(alternate, "POST", second + "/staging").status_code == 200
        assert serving_delivery(service, runner.registry, kube).deliver_one()
        other = api(alternate, "POST", second + "/verification").json()
        assert (
            api(
                alternate,
                "POST",
                second + "/decisions",
                who="operator",
                json=decision_request(other),
            ).status_code
            == 200
        )
    barrier = Barrier(2)

    def promote(path):
        barrier.wait(timeout=10)
        return api(client, "POST", path + "/deployment", who="operator")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(promote, [first, second]))
    assert sorted(r.status_code for r in responses) == ([200, 200] if same_release else [200, 409])
    assert len(api(client, "GET", "/model-services/linear").json()["history"]) == 1
