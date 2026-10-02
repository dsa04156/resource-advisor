import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation
from resource_advisor.contracts import ExecutionResult, JobRequest, State, signature
from resource_advisor.diagnostics import PhaseProfile, PhaseSample, diagnose, validate_profile
from resource_advisor.service import Rejected
from resource_advisor.store import Conflict
from resource_advisor.worker import Worker


def profile(phases=None, count=10):
    return PhaseProfile(
        job_id="job-1",
        attempt_id="attempt-1",
        result_digest=signature("fixture"),
        measurement_boundary="test-e2e",
        samples=tuple(
            PhaseSample(
                wall_seconds=0.1,
                phases_seconds=phases
                or {
                    "cpu_processing": 0.08,
                    "accelerator_compute": 0.01,
                    "host_to_device": 0.01,
                },
            )
            for _ in range(count)
        ),
    )


def test_no_gpu_utilization_shortcut_and_synthetic_abstention():
    assert diagnose()["reasons"] == ["NO_PHASE_PROFILE"]
    assert diagnose(profile(), evidence_kind="synthetic")["hypothesis"] is None
    report = diagnose(profile(), evidence_kind="hardware")
    assert report["hypothesis"] == "POSSIBLE_INPUT_SUPPLY_BOUND"
    assert report["resource_change"] is None and not report["auto_apply"]
    assert report["profile_digest"] == signature(profile())


@pytest.mark.parametrize(
    "phases,reason",
    [
        ({"accelerator_compute": 0.02}, "INCOMPLETE_PHASE_COVERAGE"),
        (
            {"accelerator_compute": 0.05, "cpu_processing": 0.05},
            "MIXED_OR_UNSTABLE_PHASE_DOMINANCE",
        ),
    ],
)
def test_missing_and_mixed_phases_abstain(phases, reason):
    assert diagnose(profile(phases), evidence_kind="hardware")["reasons"] == [reason]


def test_outlier_does_not_become_a_causal_claim():
    samples = [PhaseSample(wall_seconds=0.01, phases_seconds={"accelerator_compute": 0.01})] * 9
    samples += [PhaseSample(wall_seconds=1, phases_seconds={"file_read": 1})]
    report = diagnose(
        profile().model_copy(update={"samples": tuple(samples)}), evidence_kind="hardware"
    )
    assert report["hypothesis"] is None  # inconsistent instrumentation is incomplete


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf"), 0.2])
def test_invalid_or_overlapping_durations_rejected(seconds):
    with pytest.raises(ValidationError):
        PhaseSample(wall_seconds=0.1, phases_seconds={"accelerator_compute": seconds})


def setup(service):
    job = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "diagnostic"
    )
    backend = SchedulerDouble()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
    envelope = backend.result(row)
    result = ExecutionResult.model_validate(envelope["result"])
    phases = profile().model_copy(
        update={
            "job_id": job["job_id"],
            "attempt_id": job["attempt_id"],
            "result_digest": envelope["digest"],
            "measurement_boundary": "fixture-forward-only",
        }
    )
    envelope["phase_profile"] = phases.model_dump(mode="json")
    backend.result = lambda _: envelope
    return job, result, phases, backend


def test_worker_atomic_profile_and_project_scoped_diagnostic(service):
    job, result, phases, backend = setup(service)
    worker = Worker(service, {("team-a", "lab"): backend})
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
    with service.store.transaction() as conn:
        assert service.store.get(conn, "phase_profile", result.attempt_id)[
            "body"
        ] == phases.model_dump(mode="json")
    service.ingest("team-a", result, signature(result), phase_profile=phases)
    with pytest.raises(Conflict):
        service.ingest("team-a", result, signature(result), phase_profile=profile())
    client = TestClient(
        create_app(
            service,
            {
                hashlib.sha256(b"a").hexdigest(): Principal("team-a"),
                hashlib.sha256(b"b").hexdigest(): Principal("team-b"),
            },
        )
    )
    path = "/api/v1/compute/jobs/" + job["job_id"] + "/diagnostics"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer b"}).status_code == 404
    response = client.get(path, headers={"Authorization": "Bearer a"})
    assert response.status_code == 200
    assert response.json()["reasons"] == ["HARDWARE_EVIDENCE_REQUIRED"]


@pytest.mark.parametrize(
    "change",
    [
        {"result_digest": signature("other")},
        {"job_id": "other-job"},
        {"measurement_boundary": "kernel-only"},
        {"samples": profile(count=3).samples},
    ],
)
def test_mismatched_result_cannot_attach_diagnostics(service, change):
    job, result, phases, _ = setup(service)
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.COLLECTING, row["body"])
    with pytest.raises(Rejected, match="invalid phase"):
        service.ingest(
            "team-a", result, signature(result), phase_profile=phases.model_copy(update=change)
        )
    with service.store.transaction() as conn:
        assert service.store.get(conn, "result", result.attempt_id) is None
        assert service.store.get(conn, "phase_profile", result.attempt_id) is None
    assert service.get_job("team-a", job["job_id"])["state"] == "COLLECTING"


def test_total_boundary_cannot_disagree_with_measurement(service):
    _, result, phases, _ = setup(service)
    shifted = phases.model_copy(
        update={
            "samples": tuple(
                PhaseSample(wall_seconds=0.2, phases_seconds={"cpu_processing": 0.2})
                for _ in range(10)
            )
        }
    )
    with pytest.raises(ValueError, match="wall time"):
        validate_profile(shifted, result, "fixture-forward-only")


@pytest.mark.parametrize(
    "phase,hypothesis",
    [
        ("file_read", "POSSIBLE_IO_BOUND"),
        ("host_to_device", "POSSIBLE_TRANSFER_BOUND"),
        ("synchronization", "POSSIBLE_SYNCHRONIZATION_BOUND"),
        ("accelerator_compute", "POSSIBLE_ACCELERATOR_PATH_BOUND"),
    ],
)
def test_phase_specific_hypotheses_keep_resource_count(phase, hypothesis):
    result = diagnose(profile({phase: 0.1}), evidence_kind="hardware")
    assert result["hypothesis"] == hypothesis and result["resource_change"] is None
    assert diagnose(profile({phase: 0.1}, count=2), evidence_kind="hardware")["hypothesis"] is None


def test_durable_phase_artifact_and_mlflow_metrics(service):
    from test_artifacts import MemoryS3

    from resource_advisor.artifacts import ArtifactDelivery, S3Artifacts
    from resource_advisor.console import overview
    from resource_advisor.worker import MLflowDelivery

    _, _, phases, backend = setup(service)
    worker = Worker(service, {("team-a", "lab"): backend})
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=MemoryS3())
    assert ArtifactDelivery(service.store, storage).deliver_one()
    with service.store.transaction() as conn:
        artifact = service.store.list(conn, "artifact", "team-a")[0]["body"]
    assert json.loads(storage.read(artifact))["phase_profile"] == phases.model_dump(mode="json")
    batches = []

    def mlflow(request):
        if request.url.path.endswith("runs/search"):
            return httpx.Response(200, json={"runs": []})
        if request.url.path.endswith("runs/create"):
            return httpx.Response(200, json={"run": {"info": {"run_id": "test-run"}}})
        if request.url.path.endswith("runs/log-batch"):
            batches.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = httpx.Client(base_url="https://mlflow.invalid", transport=httpx.MockTransport(mlflow))
    assert MLflowDelivery(
        service.store, "https://mlflow.invalid", experiments={"team-a": "42"}, client=client
    ).deliver_one()
    metrics = {x["key"]: x["value"] for x in batches[0]["metrics"]}
    assert metrics["phase_seconds.cpu_processing"] == pytest.approx(0.8)
    assert "phase_seconds.file_read" not in metrics
    assert overview(service, "team-a")["jobs"]["items"][0]["diagnostics"]["reasons"] == [
        "HARDWARE_EVIDENCE_REQUIRED"
    ]
