import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_service import complete

from resource_advisor.api import Principal, create_app
from resource_advisor.contracts import ApprovalRequest, JobRequest, State, now, signature
from resource_advisor.right_sizing import lifecycle
from resource_advisor.service import NotFound
from resource_advisor.store import entities, jobs, studies, usage


def approved(service):
    for i in range(3):
        complete(service, "source-" + str(i))
    rec = service.recommend("team-a", "workload-1")
    approval = service.approve(
        "team-a",
        rec["ref"],
        ApprovalRequest(recommendation_digest=rec["digest"], candidate_ref="base"),
    )
    job = service.submit(
        "team-a",
        JobRequest(
            workload_ref="workload-1",
            candidate_ref="base",
            mode="fixed",
            approval_ref=approval["ref"],
        ),
        "approved",
    )
    return rec, approval, job


def finish_approved(service, job, *, quality=0.99):
    _, template = complete(service, "unrelated-template")
    result = template.model_copy(update={"job_id": job["job_id"], "attempt_id": job["attempt_id"]})
    result = result.model_copy(
        update={
            "measurements": result.measurements.model_copy(
                update={"quality_value": quality, "elapsed_seconds": 1.1}
            )
        }
    )
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.COLLECTING, row["body"])
    service.ingest("team-a", result, signature(result))
    return result


def test_unknown_lifecycle_is_read_only_and_abstains(service):
    with service.store.transaction() as conn:
        before = list(conn.execute(select(entities)).mappings())
    report = lifecycle(service, "team-a", "workload-1")
    assert report["state"] == "NEEDS_PROFILE"
    assert report["recommendation_ref"] is None
    assert report["additional_profiling_required"]
    assert report["milestones"][0]["observed"]
    with service.store.transaction() as conn:
        assert list(conn.execute(select(entities)).mappings()) == before
        assert not conn.execute(select(jobs)).first()
        assert not conn.execute(select(studies)).first()
    with pytest.raises(NotFound):
        lifecycle(service, "team-b", "workload-1")


def test_approved_terminal_feedback_is_atomic_and_independent(service):
    rec, approval, job = approved(service)
    assert lifecycle(service, "team-a", "workload-1")["state"] == "APPROVED"
    result = finish_approved(service, job)
    report = lifecycle(service, "team-a", "workload-1")
    assert report["state"] == "VERIFIED"
    receipt = report["feedback"][0]
    assert receipt["recommendation_ref"] == rec["ref"]
    assert receipt["approval_ref"] == approval["ref"]
    assert receipt["source_profile_refs"] == rec["ranking"][0]["evidence_refs"]
    assert job["attempt_id"] not in receipt["source_profile_refs"]
    assert receipt["relative_residual"] == pytest.approx(0.1)
    assert receipt["actual_seconds"] == 1.1
    service.ingest("team-a", result, signature(result))
    with service.store.transaction() as conn:
        assert len(service.store.list(conn, "recommendation_feedback", "team-a")) == 1
        assert conn.execute(select(usage).where(usage.c.attempt_id == job["attempt_id"])).first()


def test_constraint_failure_is_not_verified_or_zero_latency(service):
    _, _, job = approved(service)
    finish_approved(service, job, quality=0.1)
    report = lifecycle(service, "team-a", "workload-1")
    assert report["state"] == "NEEDS_RECONFIRMATION"
    receipt = report["feedback"][0]
    assert receipt["status"] == "INSUFFICIENT_EVIDENCE"
    assert receipt["actual_seconds"] is None
    assert "ACTUAL_CONSTRAINT_VIOLATION" in receipt["reasons"]


def test_cancel_without_result_preserves_censored_feedback_and_usage(service):
    _, _, job = approved(service)
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.CANCELED, row["body"])
        receipt = service.store.get(conn, "recommendation_feedback", job["attempt_id"])["body"]
        assert receipt["actual_seconds"] is None
        assert receipt["evidence_kind"] == "unavailable"
        assert receipt["terminal_state"] == "CANCELED"
        assert conn.execute(select(usage).where(usage.c.attempt_id == job["attempt_id"])).first()
    assert lifecycle(service, "team-a", "workload-1")["state"] == "EXECUTED"


def test_expired_recommendation_overrides_historical_verification(service, monkeypatch):
    _, _, job = approved(service)
    finish_approved(service, job)
    monkeypatch.setattr("resource_advisor.uncertainty.now", lambda: now() + timedelta(days=1))
    report = lifecycle(service, "team-a", "workload-1")
    assert report["state"] == "NEEDS_RECONFIRMATION"
    assert "RECOMMENDATION_EXPIRED_OR_TIME_INVALID" in report["reasons"]


def test_api_lifecycle_auth_and_project_boundary(service):
    token = "local-lifecycle-test-only"
    app = create_app(service, {hashlib.sha256(token.encode()).hexdigest(): Principal("team-a")})
    client = TestClient(app)
    path = "/api/v1/compute/workloads/workload-1/right-sizing"
    assert client.get(path).status_code == 401
    assert (
        client.get(path, headers={"Authorization": "Bearer " + token}).json()["state"]
        == "NEEDS_PROFILE"
    )


def test_production_projection_does_not_verify_synthetic_feedback(service):
    _, _, job = approved(service)
    finish_approved(service, job)
    service.accept_synthetic = False
    assert lifecycle(service, "team-a", "workload-1")["state"] != "VERIFIED"


def test_legacy_signature_is_unchanged_and_new_semantics_split_history(bundle):
    from resource_advisor.contracts import ExecutionContext, WorkloadIdentity
    from resource_advisor.policy import context_signature

    spec, candidate, variant, _ = bundle
    old_identity = spec.identity.model_dump(mode="json")
    old_context = candidate.context.model_dump(mode="json")
    assert signature(WorkloadIdentity.model_validate(old_identity)) == signature(old_identity)
    assert signature(ExecutionContext.model_validate(old_context)) == signature(old_context)
    identity = WorkloadIdentity.model_validate(
        {
            **old_identity,
            "model_name": "fixture",
            "optimizer": "adam",
            "optimizer_parameters": {"lr": 0.001},
            "input_shape_range": [[1, 1], [3, 3], [32, 64], [32, 64]],
        }
    )
    assert signature(identity) != signature(spec.identity)
    changed = candidate.model_copy(
        update={
            "context": ExecutionContext.model_validate(
                {
                    **old_context,
                    "host_cpu_model": "qualified-test-cpu",
                    "runtime_flags": {"tf32": False},
                }
            )
        }
    )
    assert context_signature(changed, variant) != context_signature(candidate, variant)
    with pytest.raises(ValueError, match="declared positive range"):
        WorkloadIdentity.model_validate({**old_identity, "input_shape_range": [[1, 1]]})
