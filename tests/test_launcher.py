import httpx
import pytest

from resource_advisor.launcher import execute


def test_launcher_preserves_idempotency_key():
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "j-1"})
        return httpx.Response(200, json={"job_id": "j-1", "state": "SUCCEEDED"})

    client = httpx.Client(base_url="https://advisor.invalid", transport=httpx.MockTransport(handle))
    result = execute(
        "https://advisor.invalid", "unused", {}, "pipeline-run-1", 10, client=client, poll_seconds=0
    )
    assert result["state"] == "SUCCEEDED"
    assert calls[0].headers["Idempotency-Key"] == "pipeline-run-1"


def test_launcher_deadline_requests_cancellation():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"job_id": "j-1", "state": "QUEUED"})

    client = httpx.Client(base_url="https://advisor.invalid", transport=httpx.MockTransport(handle))
    with pytest.raises(TimeoutError):
        execute(
            "https://advisor.invalid",
            "unused",
            {},
            "run-1",
            0.001,
            client=client,
            poll_seconds=0.002,
        )
    assert calls[-1].endswith("/j-1/cancel")


def test_owned_launcher_renews_lease_and_cancels_on_interruption():
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/heartbeat"):
            raise KeyboardInterrupt("workflow termination")
        return httpx.Response(200, json={"job_id": "j-1"})

    client = httpx.Client(base_url="https://advisor.invalid", transport=httpx.MockTransport(handle))
    with pytest.raises(KeyboardInterrupt):
        execute(
            "https://advisor.invalid",
            "unused",
            {"owner_lease_seconds": 60},
            "run-1",
            10,
            client=client,
            poll_seconds=0,
        )
    assert calls == [
        ("POST", "/api/v1/compute/jobs"),
        ("POST", "/api/v1/compute/jobs/j-1/heartbeat"),
        ("POST", "/api/v1/compute/jobs/j-1/cancel"),
    ]
