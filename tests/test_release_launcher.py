import importlib

import httpx
import pytest


def test_train_reconciles_uncertain_submit_and_waits_for_registry():
    launcher = importlib.import_module("resource_advisor.release_launcher")
    submits, polls = [], []

    def server(request):
        if request.method == "POST":
            submits.append((request.headers["Idempotency-Key"], request.content))
            if len(submits) == 1:
                raise httpx.ReadTimeout("accepted but response lost", request=request)
            return httpx.Response(200, json={"ref": "mlc-one", "state": "TRAINING"})
        polls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "ref": "mlc-one",
                "state": "WAITING_FOR_ARTIFACT" if len(polls) == 1 else "REGISTERED",
            },
        )

    client = httpx.Client(base_url="https://api.invalid", transport=httpx.MockTransport(server))
    result = launcher.train(client, "policy-one", "same-training-key", 30, poll_seconds=0)
    assert result == "mlc-one"
    assert submits[0] == submits[1]
    assert len(polls) == 2


@pytest.mark.parametrize(
    "state", ["TRAINING_FAILED", "QUALITY_REJECTED", "REGISTRY_RECONCILIATION_REQUIRED"]
)
def test_unsuccessful_lifecycle_never_emits_candidate(state):
    launcher = importlib.import_module("resource_advisor.release_launcher")
    client = httpx.Client(
        base_url="https://api.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ref": "mlc-one", "state": state})
        ),
    )
    with pytest.raises(RuntimeError, match=state):
        launcher.train(client, "policy-one", "same-key", 30, poll_seconds=0)


def test_stage_uses_only_candidate_apis_and_waits_for_saved_verification():
    launcher = importlib.import_module("resource_advisor.release_launcher")
    paths, polls = [], []

    def server(request):
        paths.append((request.method, request.url.path))
        if request.url.path.endswith("/verification"):
            return httpx.Response(
                200,
                json={
                    "ref": "mrl-one",
                    "state": "PENDING_APPROVAL",
                    "verification": {"model_uri": "models:/ra-project-linear/1"},
                },
            )
        if request.method == "GET":
            polls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "ref": "mrl-one",
                "state": "STAGING",
                "verification": None,
                "staging": {"state": "READY" if polls else "PENDING"},
            },
        )

    client = httpx.Client(base_url="https://api.invalid", transport=httpx.MockTransport(server))
    assert (
        launcher.stage(
            client, "mlc-one", "release-policy", "stable-release-key", 30, poll_seconds=0
        )
        == "mrl-one"
    )
    assert paths == [
        ("POST", "/api/v1/compute/model-releases"),
        ("POST", "/api/v1/compute/model-releases/mrl-one/staging"),
        ("GET", "/api/v1/compute/model-releases/mrl-one"),
        ("POST", "/api/v1/compute/model-releases/mrl-one/verification"),
    ]


def test_output_ref_is_raw_text_and_uses_no_operator_secret(tmp_path, monkeypatch, capsys):
    launcher = importlib.import_module("resource_advisor.release_launcher")

    # CLI filesystem/output contract: HTTP transport is the external seam.
    def server(request):
        assert request.headers["Authorization"] == "Bearer project-user-fixture"
        return httpx.Response(200, json={"ref": "mlc-one", "state": "REGISTERED"})

    client = httpx.Client(
        base_url="https://api.invalid",
        transport=httpx.MockTransport(server),
        headers={"Authorization": "Bearer project-user-fixture"},
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    output = tmp_path / "outputs" / "ref"
    monkeypatch.setenv("RA_API_TOKEN", "project-user-fixture")
    monkeypatch.setattr(
        "sys.argv",
        [
            "release_launcher",
            "train",
            "--api-url",
            "https://api.invalid",
            "--policy-ref",
            "policy-one",
            "--idempotency-key",
            "key-one",
            "--output",
            str(output),
        ],
    )
    launcher.main()
    assert output.read_text() == "mlc-one"
    assert "project-user-fixture" not in capsys.readouterr().out
