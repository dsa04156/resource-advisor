"""Executable HTTP-to-result demo. Every performance value is SYNTHETIC."""

import hashlib
import json

import httpx
from fastapi.testclient import TestClient
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation
from resource_advisor.contracts import State
from resource_advisor.worker import MLflowDelivery, Worker


def test_http_to_recommendation_and_mlflow_retry(service):
    token = "synthetic-demo-token"
    digest = hashlib.sha256(token.encode()).hexdigest()
    client = TestClient(create_app(service, {digest: Principal("team-a")}))
    headers = {"Authorization": "Bearer " + token}
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    prefix = "/api/v1/compute"
    recorded_jobs = []
    for index in range(3):
        response = client.post(
            prefix + "/jobs",
            headers={**headers, "Idempotency-Key": f"demo-{index}"},
            json={"workload_ref": "workload-1", "candidate_ref": "base"},
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]
        worker.submit_one()
        backend.observation = Observation(State.COLLECTING)
        worker.reconcile_all()
        job = client.get(prefix + "/jobs/" + job_id, headers=headers).json()
        assert job["state"] == "SUCCEEDED"
        recorded_jobs.append(job_id)
    rec = client.post(
        prefix + "/recommendations", headers=headers, json={"workload_ref": "workload-1"}
    ).json()
    assert rec["status"] == "MEASURED_RECOMMENDATION"
    approved = client.post(
        prefix + "/recommendations/" + rec["ref"] + "/approve",
        headers=headers,
        json={"recommendation_digest": rec["digest"], "candidate_ref": rec["candidate_ref"]},
    )
    assert approved.status_code == 200
    calls = []
    runs = {}

    def mlflow(request):
        calls.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path.endswith("runs/search"):
            assert body["experiment_ids"] == ["team-a-experiment"]
            return httpx.Response(200, json={"runs": []})
        if request.url.path.endswith("runs/create"):
            assert body["experiment_id"] == "team-a-experiment"
            attempt = next(
                t["value"] for t in body["tags"] if t["key"] == "resource_advisor.attempt_id"
            )
            runs[attempt] = "run-" + str(len(runs))
            return httpx.Response(200, json={"run": {"info": {"run_id": runs[attempt]}}})
        return httpx.Response(200, json={})

    delivery = MLflowDelivery(
        service.store,
        "https://mlflow.invalid",
        experiments={"team-a": "team-a-experiment"},
        client=httpx.Client(
            base_url="https://mlflow.invalid", transport=httpx.MockTransport(mlflow)
        ),
    )
    assert all(delivery.deliver_one() for _ in range(3))
    assert len(runs) == 3
    assert not delivery.deliver_one()
    with service.store.transaction() as conn:
        links = service.store.list(conn, "tracking", "team-a")
        assert len(links) == 3
    report = {
        "evidence_kind": "synthetic",
        "real_schedulers_used": False,
        "real_gpu_used": False,
        "http_jobs_completed": len(recorded_jobs),
        "independent_runs": rec["ranking"][0]["independent_runs"],
        "recommendation": rec["status"],
        "approval": "accepted",
        "mlflow_mock_runs": len(runs),
    }
    print(json.dumps(report, indent=2))
