"""Synthetic fault fixtures; hardware-shaped fields are not hardware evidence."""

import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update
from test_service import complete
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.contracts import ApprovalRequest, JobRequest, now, signature
from resource_advisor.service import Rejected
from resource_advisor.store import entities, jobs, studies
from resource_advisor.uncertainty import assess_recommendation, shadow_report
from resource_advisor.worker import Worker


def recommendation(service):
    for i in range(3):
        complete(service, f"initial-{i}")
    return service.recommend("team-a", "workload-1")


def approve(service, rec):
    return service.approve(
        "team-a",
        rec["ref"],
        ApprovalRequest(recommendation_digest=rec["digest"], candidate_ref="base"),
    )


def test_drift_latches_old_recommendation_and_new_lookup_requires_fresh_evidence(service):
    rec = recommendation(service)
    approval = approve(service, rec)
    drift = []
    for i in range(3):
        row, _ = complete(service, f"drift-{i}", elapsed=1.5)
        drift.append(row["attempt_id"])
        status = service.recommendation_validity("team-a", rec["ref"])
        assert status["reusable"] is (i < 2)
    assert status["drift_evidence_refs"] == drift
    with pytest.raises(Rejected, match="CONSECUTIVE_RESIDUAL_DRIFT"):
        approve(service, rec)
    with pytest.raises(Rejected, match="CONSECUTIVE_RESIDUAL_DRIFT"):
        service.submit(
            "team-a",
            JobRequest(
                workload_ref="workload-1", candidate_ref="base", approval_ref=approval["ref"]
            ),
            "stale-approved",
        )
    lookup = service.recommend("team-a", "workload-1")
    assert lookup["status"] == "NEEDS_RECONFIRMATION" and not lookup["measured"]
    assert lookup["drift_evidence_refs"]["base"] == drift
    for i in range(3):
        complete(service, f"fresh-{i}", elapsed=1.5)
    fresh = service.recommend("team-a", "workload-1")
    assert fresh["ranking"][0]["mean_seconds"] == 1.5
    assert fresh["ranking"][0]["independent_runs"] == 6
    assert set(fresh["ranking"][0]["evidence_refs"]).isdisjoint(rec["ranking"][0]["evidence_refs"])
    assert approve(service, fresh)
    assert not service.recommendation_validity("team-a", rec["ref"])["reusable"]


def test_worker_rechecks_previously_approved_queued_submission(service):
    rec = recommendation(service)
    approval = approve(service, rec)
    queued = service.submit(
        "team-a",
        JobRequest(workload_ref="workload-1", candidate_ref="base", approval_ref=approval["ref"]),
        "queued",
    )
    for i in range(3):
        complete(service, f"drift-{i}", elapsed=2)
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    while worker.submit_one():
        pass
    assert backend.submissions == 0
    job = service.get_job("team-a", queued["job_id"])
    assert job["state"] == "FAILED"
    assert "RECOMMENDATION_RECHECK_REQUIRED" in job["error"]


def test_mixed_residuals_are_not_three_consecutive_excursions(service):
    rec = recommendation(service)
    for i, value in enumerate([1.6, 1.0, 1.6, 0.6, 1.0]):
        complete(service, f"mixed-{i}", elapsed=value)
    assert service.recommendation_validity("team-a", rec["ref"])["reusable"]


def test_post_recommendation_quality_failure_blocks_reuse(service):
    rec = recommendation(service)
    failed, _ = complete(service, "quality-regressed", quality=0.1)
    report = service.recommendation_validity("team-a", rec["ref"])
    assert "POST_RECOMMENDATION_CONSTRAINT_FAILURE" in report["reasons"]
    assert report["constraint_failure_refs"] == [failed["attempt_id"]]
    assert report["residuals"] == []


def test_support_age_and_scope_rechecked_without_mutating_recommendation(service):
    rec = recommendation(service)
    with service.store.transaction() as conn:
        old = service.store.get(conn, "recommendation", rec["ref"])["digest"]
        report = assess_recommendation(service, conn, "team-a", rec, at=now() + timedelta(days=8))
        assert "SUPPORTING_PROFILE_STALE_OR_FUTURE" in report["reasons"]
        assert "CAPABILITY_STALE" in report["reasons"]
        assert service.store.get(conn, "recommendation", rec["ref"])["digest"] == old


@pytest.fixture
def shadow_fixture(service, bundle):
    # These in-memory/dedicated-test-DB rows deliberately mimic hardware records
    # solely to exercise provenance validation; they are never published as measurements.
    source, _ = complete(service, "source")
    cutoff = now()
    target, _ = complete(service, "target", elapsed=1.2)
    plan = {
        "ref": "shadow-plan",
        "study_ref": "shadow-study",
        "created_at": cutoff.isoformat(),
        "workload_digest": signature(bundle[0]),
        "choice": {
            "predictions": [
                {
                    "candidate_ref": "base",
                    "measured": False,
                    "predicted_elapsed_seconds": 1.0,
                    "posterior_interval_seconds": [0.9, 1.1],
                }
            ],
            "surrogate": {"training_run_ids": [source["attempt_id"]]},
        },
    }
    with service.store.transaction() as conn:
        for job in [source, target]:
            result = dict(service.store.get(conn, "result", job["attempt_id"])["body"])
            result["evidence_kind"] = "hardware"
            conn.execute(
                update(entities)
                .where(entities.c.kind == "result", entities.c.ref == job["attempt_id"])
                .values(body=result, digest=signature(result))
            )
        row = service.store.job(conn, target["job_id"])
        body = dict(row["body"])
        body["request"] = dict(body["request"], probe_plan_ref=plan["ref"])
        conn.execute(update(jobs).where(jobs.c.id == row["id"]).values(body=body))
        service.store.put(conn, "probe_plan", plan["ref"], "team-a", plan)
        conn.execute(
            insert(studies).values(
                id="shadow-study",
                project="team-a",
                idempotency_key="shadow-study",
                request_digest=signature("fixture"),
                version=1,
                state="COMPLETED",
                body={
                    "spec": bundle[0].model_dump(mode="json"),
                    "observations": [
                        {
                            "attempt_id": target["attempt_id"],
                            "plan_ref": plan["ref"],
                            "candidate_ref": "base",
                        }
                    ],
                },
            )
        )
    return source, target, plan


def test_shadow_report_measures_out_of_sample_error_and_keeps_workload_scope(
    service, shadow_fixture
):
    source, target, _ = shadow_fixture
    report = shadow_report(service.store, "team-a")
    assert not report["excluded"]
    metric = report["by_workload"][0]
    assert metric["count"] == 1 and metric["empirical_interval_coverage"] == 0
    assert metric["mean_absolute_relative_error"] == pytest.approx(1 / 6)
    assert report["evaluated"][0]["training_run_ids"] == [source["attempt_id"]]
    assert target["attempt_id"] not in report["evaluated"][0]["training_run_ids"]
    assert report["workload_holdout"]["status"] == "NOT_QUALIFIED"
    assert not shadow_report(service.store, "other-project")["evaluated"]


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("target_in_training", "TRAIN_TARGET_OVERLAP_OR_MISSING_PROVENANCE"),
        ("forecast_after_target", "FORECAST_NOT_BEFORE_TARGET"),
        ("future_training", "TRAINING_SCOPE_OR_TIME_LEAKAGE"),
        ("wrong_workload", "TRAINING_SCOPE_OR_TIME_LEAKAGE"),
        ("synthetic_training", "TRAINING_NOT_VALIDATED_HARDWARE"),
        ("synthetic_target", "TARGET_NOT_COMPARABLE_HARDWARE_RESULT"),
        ("wrong_context", "TARGET_NOT_COMPARABLE_HARDWARE_RESULT"),
        ("inverted_interval", "INVALID_FORECAST"),
    ],
)
def test_shadow_audit_rejects_leakage_and_invalid_provenance(
    service, shadow_fixture, fault, reason
):
    source, target, plan = shadow_fixture
    with service.store.transaction() as conn:
        if fault == "target_in_training":
            plan["choice"]["surrogate"]["training_run_ids"].append(target["attempt_id"])
        elif fault == "forecast_after_target":
            plan["created_at"] = (now() + timedelta(seconds=1)).isoformat()
        elif fault == "inverted_interval":
            plan["choice"]["predictions"][0]["posterior_interval_seconds"] = [2, 1]
        elif fault in {"future_training", "wrong_workload"}:
            body = dict(service.store.job(conn, source["job_id"])["body"])
            body["finished_at" if fault == "future_training" else "workload_signature"] = (
                (now() + timedelta(seconds=1)).isoformat()
                if fault == "future_training"
                else signature("other")
            )
            conn.execute(update(jobs).where(jobs.c.id == source["job_id"]).values(body=body))
        else:
            ref = source["attempt_id"] if fault == "synthetic_training" else target["attempt_id"]
            body = dict(service.store.get(conn, "result", ref)["body"])
            body["context_signature" if fault == "wrong_context" else "evidence_kind"] = (
                signature("other") if fault == "wrong_context" else "synthetic"
            )
            conn.execute(
                update(entities)
                .where(entities.c.kind == "result", entities.c.ref == ref)
                .values(body=body)
            )
        conn.execute(
            update(entities)
            .where(entities.c.kind == "probe_plan", entities.c.ref == plan["ref"])
            .values(body=plan)
        )
    report = shadow_report(service.store, "team-a")
    assert not report["evaluated"] and report["excluded"][0]["reason"] == reason


def test_uncertainty_endpoints_require_identity_and_hide_other_projects(service):
    rec = recommendation(service)
    app = create_app(
        service,
        {
            hashlib.sha256(b"local-a").hexdigest(): Principal("team-a"),
            hashlib.sha256(b"local-b").hexdigest(): Principal("team-b"),
        },
    )
    with TestClient(app) as client:
        path = f"/api/v1/compute/recommendations/{rec['ref']}/validity"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer local-b"}).status_code == 404
        assert client.get(path, headers={"Authorization": "Bearer local-a"}).json()["reusable"]
        assert (
            client.get(
                "/api/v1/compute/uncertainty/shadow-report",
                headers={"Authorization": "Bearer local-b"},
            ).json()["evaluated"]
            == []
        )
