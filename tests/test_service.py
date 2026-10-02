import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from resource_advisor.api import Principal, create_app
from resource_advisor.contracts import ExecutionResult, JobRequest, Measurements, State, signature
from resource_advisor.service import NotFound, Rejected
from resource_advisor.store import Conflict, jobs, outbox


def complete(service, key="one", elapsed=1, quality=0.99, digest_override=None):
    job = service.submit("team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), key)
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.COLLECTING, row["body"])
    result = ExecutionResult(
        job_id=job["job_id"],
        attempt_id=job["attempt_id"],
        epoch=1,
        workload_signature=job["workload_signature"],
        context_signature=job["context_signature"],
        outcome="COMPLETED",
        evidence_kind="synthetic",
        measurements=Measurements(
            elapsed_seconds=elapsed,
            peak_memory_mib=256,
            quality_value=quality,
            sample_count=10,
            work_units=10,
        ),
    )
    return service.ingest("team-a", result, digest_override or signature(result)), result


def test_idempotency_and_conflicting_request(service):
    request = JobRequest(workload_ref="workload-1", candidate_ref="base")
    first = service.submit("team-a", request, "key")
    assert service.submit("team-a", request, "key") == first
    with pytest.raises(Conflict):
        service.submit("team-a", request.model_copy(update={"mode": "fixed"}), "key")
    with service.store.transaction() as conn:
        assert len(conn.execute(select(jobs)).all()) == 1
        assert len(conn.execute(select(outbox)).all()) == 1


def test_consent_does_not_silently_enable_unimplemented_profiling(service):
    with pytest.raises(Rejected, match="not enabled"):
        service.submit(
            "team-a",
            JobRequest(workload_ref="workload-1", candidate_ref="base", mode="pilot"),
            "key",
        )


def test_cross_project_hidden(service):
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "key"
    )
    with pytest.raises(NotFound):
        service.get_job("team-b", job["job_id"])


def test_profile_needs_independent_runs(service):
    for index in range(2):
        complete(service, str(index))
    assert service.recommend("team-a", "workload-1")["status"] == "NEEDS_PROFILE"
    complete(service, "third")
    rec = service.recommend("team-a", "workload-1")
    assert rec["status"] == "MEASURED_RECOMMENDATION"
    assert rec["ranking"][0]["independent_runs"] == 3


def test_low_quality_not_a_profile(service):
    job, _ = complete(service, quality=0.1)
    assert job["state"] == "SUCCEEDED"  # execution success is separate from quality gate
    with service.store.transaction() as conn:
        assert not service.store.list(conn, "profile", "team-a")


def test_digest_failure_is_not_success(service):
    job, _ = complete(service, digest_override=signature("wrong"))
    assert job["state"] == "RESULT_INVALID"


def test_duplicate_result_immutable_and_no_duplicate_usage(service):
    job, result = complete(service)
    assert service.ingest("team-a", result, signature(result)) == job
    with pytest.raises(Conflict):
        service.ingest(
            "team-a", result.model_copy(update={"error_code": "changed"}), signature(result)
        )


def test_stale_attempt(service):
    _, result = complete(service)
    with pytest.raises(Conflict, match="stale"):
        service.ingest("team-a", result.model_copy(update={"epoch": 2}), signature(result))


def test_production_rejects_synthetic(service):
    service.accept_synthetic = False
    job, _ = complete(service)
    assert job["state"] == "RESULT_INVALID"


def test_api_auth_operator_and_project(service, bundle):
    token = "local-test-only"
    app = create_app(service, {hashlib.sha256(token.encode()).hexdigest(): Principal("team-a")})
    client = TestClient(app)
    prefix = "/api/v1/compute"
    assert client.get(prefix + "/jobs").status_code == 401
    headers = {"Authorization": "Bearer " + token}
    assert (
        client.post(
            prefix + "/capabilities", headers=headers, json=bundle[3].model_dump(mode="json")
        ).status_code
        == 403
    )
    data = bundle[0].model_dump(mode="json")
    data["project_ref"] = "team-b"
    assert client.post(prefix + "/workloads", headers=headers, json=data).status_code == 422
    assert client.get(prefix + "/jobs", headers=headers).status_code == 200


def test_immutable_registry(service, bundle):
    variant = bundle[2].model_copy(update={"command": ("different",)})
    with pytest.raises(Conflict):
        service.register("variant", variant, "team-a")
