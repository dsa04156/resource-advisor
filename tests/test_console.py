import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select
from test_inventory import config, source
from test_service import complete
from test_worker import setup

from resource_advisor.api import Principal, create_app
from resource_advisor.console import overview, recommendation_evidence
from resource_advisor.contracts import (
    ApprovalRequest,
    ExecutionResult,
    JobRequest,
    Measurements,
    State,
    now,
    signature,
)
from resource_advisor.inventory import InventoryCollector, fresh_view, save_inventory
from resource_advisor.service import NotFound
from resource_advisor.store import entities, jobs


def client_for(service):
    return TestClient(
        create_app(
            service,
            {
                hashlib.sha256(b"a").hexdigest(): Principal("team-a"),
                hashlib.sha256(b"b").hexdigest(): Principal("team-b"),
            },
        )
    )


def test_console_auth_and_project_isolation_without_side_effects(service):
    complete(service)
    rec = service.recommend("team-a", "workload-1")
    snapshot = InventoryCollector(config(), execute=source()).collect()
    save_inventory(service.store, "team-a", snapshot)
    with service.store.transaction() as conn:
        before = conn.execute(select(func.count()).select_from(entities)).scalar_one()
    with client_for(service) as client:
        assert client.get("/console").status_code == 200
        assert "default-src 'none'" in client.get("/console").headers["content-security-policy"]
        for asset in ("console.js", "console.css"):
            assert client.get("/console/" + asset).status_code == 200
        assert client.get("/console/not-a-file").status_code == 404
        assert client.get("/api/v1/compute/overview").status_code == 401
        a = client.get("/api/v1/compute/overview", headers={"Authorization": "Bearer a"})
        assert a.status_code == 200 and a.headers["cache-control"] == "no-store"
        assert a.json()["jobs"]["total"] == 1
        assert a.json()["history"]["total"] == 1
        assert a.json()["recommendations"]["total"] == 1
        displayed = a.json()["recommendations"]["items"][0]
        assert displayed["digest"] == rec["digest"]
        assert displayed["selected_context"] is None  # Insufficient history abstains.
        assert a.json()["inventory"][0]["ref"] == snapshot["ref"]
        b = client.get("/api/v1/compute/overview", headers={"Authorization": "Bearer b"}).json()
        assert b["inventory"] == [] and b["job_counts"] == {}
        assert all(
            b[k]["total"] == 0 for k in ("jobs", "compatibility", "history", "recommendations")
        )
        assert (
            client.get(
                f"/api/v1/compute/recommendations/{rec['ref']}/evidence",
                headers={"Authorization": "Bearer b"},
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/v1/compute/overview?jobs_page=-1", headers={"Authorization": "Bearer a"}
            ).status_code
            == 422
        )
    with service.store.transaction() as conn:
        assert conn.execute(select(func.count()).select_from(entities)).scalar_one() == before


def test_overview_pagination_and_latest_inventory(service):
    for i in range(26):
        service.submit(
            "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), str(i)
        )
    old = InventoryCollector(config(), execute=source()).collect()
    old["collected_at"] = (now() - timedelta(minutes=10)).isoformat()
    save_inventory(service.store, "team-a", old)
    new = InventoryCollector(config(), execute=source(pods_fail=True)).collect()
    save_inventory(service.store, "team-a", new)
    first = overview(service, "team-a")
    second = overview(service, "team-a", jobs_page=1)
    assert first["jobs"]["total"] == 26 and len(first["jobs"]["items"]) == 25
    assert first["jobs"]["has_next"] and len(second["jobs"]["items"]) == 1
    assert first["jobs"]["items"][0]["job_id"] != second["jobs"]["items"][0]["job_id"]
    assert first["inventory"][0]["ref"] == new["ref"]
    assert (
        first["inventory"][0]["nodes"][0]["resources"]["cpu"]["request_headroom"]["value"] is None
    )
    assert fresh_view(old)["status"] == "stale"


def test_optional_anonymous_project_keeps_operator_and_other_projects_protected(service):
    credentials = {
        hashlib.sha256(b"a").hexdigest(): Principal("team-a", operator=True),
        hashlib.sha256(b"b").hexdigest(): Principal("team-b"),
    }
    with TestClient(create_app(service, credentials, anonymous_project="team-a")) as client:
        assert client.get("/console/session").json() == {
            "authentication_required": False,
            "project_ref": "team-a",
        }
        assert client.get("/api/v1/compute/overview").json()["project_ref"] == "team-a"
        assert (
            client.get("/api/v1/compute/overview", headers={"Authorization": "Bearer b"}).json()[
                "project_ref"
            ]
            == "team-b"
        )
        assert (
            client.get(
                "/api/v1/compute/overview", headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        assert client.post("/api/v1/compute/capabilities", json={}).status_code == 403
        response = client.post(
            "/api/v1/compute/jobs",
            json={"workload_ref": "workload-1", "candidate_ref": "base"},
            headers={"Idempotency-Key": "anonymous"},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        assert (
            client.get(
                f"/api/v1/compute/jobs/{job_id}", headers={"Authorization": "Bearer b"}
            ).status_code
            == 404
        )
    with TestClient(create_app(service, credentials)) as client:
        assert client.get("/console/session").json()["authentication_required"] is True
        assert client.get("/api/v1/compute/overview").status_code == 401
    with pytest.raises(ValueError):
        create_app(service, credentials, anonymous_project="unconfigured")


def test_contract_view_rechecks_expiry_without_promoting_inventory(service, monkeypatch):
    before = overview(service, "team-a")["compatibility"]["items"][0]["candidates"][0]
    assert before["contract_compatible_now"] is True
    later = now() + timedelta(hours=1)
    monkeypatch.setattr("resource_advisor.policy.now", lambda: later)
    after = overview(service, "team-a")["compatibility"]["items"][0]["candidates"][0]
    assert after["verification"] == "MODEL_VERIFIED"  # Historical validation survives expiry.
    assert after["contract_compatible_now"] is False
    assert "CAPABILITY_STALE" in after["reasons"]


def test_recommendation_comparison_requires_approved_independent_measured_result(service):
    for i in range(3):
        complete(service, str(i), elapsed=2)
    rec = service.recommend("team-a", "workload-1")
    displayed = overview(service, "team-a")["recommendations"]["items"][0]
    assert displayed["digest"] == rec["digest"]
    assert displayed["selected_context"]["max_run_seconds"] > 0
    initial = recommendation_evidence(service, "team-a", rec["ref"])
    assert len(initial["evidence_runs"]) == 3 and initial["approved_executions"] == []
    approval = service.approve(
        "team-a",
        rec["ref"],
        ApprovalRequest(recommendation_digest=rec["digest"], candidate_ref="base"),
    )
    job = service.submit(
        "team-a",
        JobRequest(workload_ref="workload-1", candidate_ref="base", approval_ref=approval["ref"]),
        "independent",
    )
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.COLLECTING, row["body"])
    # Contract fixture with explicit evidence classification, not a hardware experiment.
    result = ExecutionResult(
        job_id=job["job_id"],
        attempt_id=job["attempt_id"],
        epoch=1,
        workload_signature=job["workload_signature"],
        context_signature=job["context_signature"],
        outcome="COMPLETED",
        evidence_kind="hardware",
        measurements=Measurements(
            elapsed_seconds=3, peak_memory_mib=10, quality_value=1, sample_count=10, work_units=10
        ),
    )
    service.ingest("team-a", result, signature(result))
    comparison = recommendation_evidence(service, "team-a", rec["ref"])["approved_executions"][0]
    assert comparison["actual_seconds"] == 3 and comparison["signed_error_seconds"] == 1
    assert comparison["job"]["attempt_id"] not in {
        e["attempt_id"] for e in initial["evidence_runs"]
    }
    with pytest.raises(NotFound):
        recommendation_evidence(service, "team-b", rec["ref"])


def test_synthetic_result_not_presented_as_independent_hardware_comparison(service):
    for i in range(3):
        complete(service, str(i))
    rec = service.recommend("team-a", "workload-1")
    with service.store.transaction() as conn:
        approval = {"recommendation_ref": rec["ref"]}
        service.store.put(conn, "approval", "test-approval", "team-a", approval)
        row = conn.execute(select(jobs).where(jobs.c.project == "team-a")).mappings().first()
        body = dict(row["body"])
        body["request"] = dict(body["request"], approval_ref="test-approval")
        conn.execute(
            insert(jobs).values(
                id="reused-test-job",
                project="team-a",
                idempotency_key="test-copy",
                state="SUCCEEDED",
                epoch=1,
                version=1,
                body=body,
            )
        )
    comparison = recommendation_evidence(service, "team-a", rec["ref"])["approved_executions"][0]
    assert comparison["actual_seconds"] is None and comparison["signed_error_seconds"] is None


def test_backend_confirmation_timestamp_is_recorded_only_after_success(service):
    from resource_advisor.backends import BackendError

    backend, worker, job = setup(service)
    worker.submit_one()
    worker.reconcile_all()
    first = overview(service, "team-a")["jobs"]["items"][0]["backend_observed_at"]
    assert first is not None

    def fail(_):
        raise BackendError("test outage")

    backend.status = fail
    worker.reconcile_all()
    current = overview(service, "team-a")["jobs"]["items"][0]
    assert current["backend_observed_at"] == first
    assert current["last_observation_error"] == "BackendError"
