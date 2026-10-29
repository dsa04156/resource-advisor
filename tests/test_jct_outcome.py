"""Completion seam: frozen prediction vs native JCT, stored and tracked in MLflow.

The scheduler double and the MLflow MockTransport are software doubles; native
timestamps below are injected observations, not hardware measurements.
"""

from datetime import timedelta

import httpx
import pytest
import test_heterogeneous_recommendation as hetero
from test_heterogeneous_recommendation import (
    jct_profile,
    register,
    scheduling_profile,
)
from test_tracking import TrackingServer, delivery, retry_now
from test_worker import SchedulerDouble

from resource_advisor.backends import Observation
from resource_advisor.contracts import JobRequest, State, now
from resource_advisor.recommendation import decision_linkage
from resource_advisor.worker import Worker

pair = hetero.pair  # shared fixture


@pytest.fixture
def scheduled(service, pair):
    spec, variant, caps = pair
    profile = scheduling_profile(recommendation={"policy": "profile_only"})
    register(
        service,
        ("scheduling_profile", profile),
        ("jct_profile", jct_profile(spec, variant, caps["base"], "base", 9.0)),
        ("jct_profile", jct_profile(spec, variant, caps["spare"], "spare", 3.0)),
    )
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    request = JobRequest(workload_ref=spec.ref, scheduling_profile_ref=profile.ref)
    job = service.submit("team-a", request, "outcome-1")
    assert worker.submit_one()
    return job["job_id"], backend, worker


def finish(backend, worker, state=State.COLLECTING, *, jct=8.0, submitted=True, **extra):
    t0 = now() - timedelta(seconds=jct + 1)
    backend.observation = Observation(
        state,
        submitted_at=t0.isoformat() if submitted else None,
        started_at=(t0 + timedelta(seconds=1)).isoformat(),
        execution_started_at=(t0 + timedelta(seconds=2)).isoformat(),
        finished_at=(t0 + timedelta(seconds=jct)).isoformat(),
        **extra,
    )
    worker.reconcile_all()


def outcome(service, job_id):
    return decision_linkage(service, "team-a", job_id)["outcome"]


def test_completion_keeps_the_frozen_prediction_and_native_jct_error(service, pair, scheduled):
    job_id, backend, worker = scheduled
    spec, variant, caps = pair
    # Evidence that arrives after acceptance cannot rewrite the frozen prediction.
    register(service, ("jct_profile", jct_profile(spec, variant, caps["spare"], "spare", 0.5)))
    finish(backend, worker, jct=8.0)
    link = decision_linkage(service, "team-a", job_id)
    assert link["state"] == "SUCCEEDED"
    result = link["outcome"]
    assert result["policy"] == "profile_only" and result["candidate_ref"] == "spare"
    assert result["prediction"]["predicted_jct_seconds"] == 6.0  # 2 + 1 + 3, frozen
    assert result["prediction"]["evaluated_at"] <= link["native_timestamps"]["created_at"]
    assert result["prediction"]["breakdown"]["compute_seconds"] == 3.0
    assert result["actual"]["status"] == "OBSERVED"
    assert result["actual"]["jct_seconds"] == pytest.approx(8.0)
    assert result["error"]["signed_seconds"] == pytest.approx(2.0)
    assert result["error"]["absolute_seconds"] == pytest.approx(2.0)
    assert result["error"]["relative"] == pytest.approx(0.25)
    assert result["plan_reasons"] and result["recommendation_status"] == "SELECTED"


@pytest.mark.parametrize("state", [State.FAILED, State.CANCELED])
def test_failed_or_cancelled_attempts_have_no_fabricated_jct(service, scheduled, state):
    job_id, backend, worker = scheduled
    finish(backend, worker, state, error="INJECTED_TEST_FAILURE")
    result = outcome(service, job_id)
    assert result["actual"]["status"] == "NOT_COMPLETED"
    assert result["actual"]["jct_seconds"] is None and result["error"] is None
    assert str(state) in result["actual"]["reason"]
    assert result["prediction"]["predicted_jct_seconds"] == 6.0


def test_missing_native_submit_time_is_reported_not_guessed(service, scheduled):
    job_id, backend, worker = scheduled
    finish(backend, worker, submitted=False)
    result = outcome(service, job_id)
    assert result["actual"]["status"] == "TIMESTAMPS_MISSING"
    assert result["actual"]["jct_seconds"] is None and result["error"] is None


def metrics(server):
    return {m["key"]: m for b in server.batches for m in b["metrics"]}


def tags(server):
    return {t["key"]: t["value"] for b in server.batches for t in b["tags"]}


def test_mlflow_receives_bounded_outcome_metrics_and_tags(service, scheduled):
    job_id, backend, worker = scheduled
    finish(backend, worker, jct=8.0)
    server = TrackingServer()
    assert delivery(service, server).deliver_one()
    logged = metrics(server)
    assert logged["recommendation.predicted_jct_seconds"]["value"] == 6.0
    assert logged["recommendation.actual_jct_seconds"]["value"] == pytest.approx(8.0)
    assert logged["recommendation.error_signed_seconds"]["value"] == pytest.approx(2.0)
    stamps = {m["timestamp"] for m in logged.values()}
    assert len(stamps) == 1 and None not in stamps  # one terminal observation time
    tagged = tags(server)
    assert tagged["recommendation.policy"] == "profile_only"
    assert tagged["recommendation.candidate_ref"] == "spare"
    assert tagged["recommendation.outcome_status"] == "OBSERVED"
    assert all(len(v) <= 500 for v in tagged.values())


def test_failed_attempt_tracks_status_without_jct_metrics(service, scheduled):
    job_id, backend, worker = scheduled
    finish(backend, worker, State.FAILED, error="INJECTED_TEST_FAILURE")
    server = TrackingServer()
    assert delivery(service, server).deliver_one()
    assert not any(k.startswith("recommendation.") for k in metrics(server))
    assert tags(server)["recommendation.outcome_status"] == "NOT_COMPLETED"


def test_mlflow_outage_never_undoes_the_completed_job_or_outcome(service, scheduled):
    job_id, backend, worker = scheduled
    finish(backend, worker, jct=8.0)

    def down(request):
        return httpx.Response(503, json={"error": "injected outage"})

    assert delivery(service, down).deliver_one()
    assert service.get_job("team-a", job_id)["state"] == "SUCCEEDED"
    saved = outcome(service, job_id)
    assert saved["actual"]["jct_seconds"] == pytest.approx(8.0)
    retry_now(service)
    server = TrackingServer()
    assert delivery(service, server).deliver_one()
    assert len(server.runs) == 1
    assert metrics(server)["recommendation.actual_jct_seconds"]["value"] == pytest.approx(8.0)
    assert outcome(service, job_id) == saved
