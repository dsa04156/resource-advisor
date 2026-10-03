"""Synthetic cgroup files and scheduler doubles; not lab performance evidence."""

import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient
from test_artifacts import MemoryS3
from test_tracking import TrackingServer, delivery
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.artifacts import ArtifactDelivery, S3Artifacts
from resource_advisor.backends import Observation, identity_environment
from resource_advisor.contracts import ExecutionResult, JobRequest, RuntimeVariant, State, signature
from resource_advisor.load_context import (
    PROVIDER,
    CgroupReader,
    LoadReading,
    LoadTrace,
    flat_counters,
    io_bytes,
    pressure_total,
    summarize,
)
from resource_advisor.policy import context_signature
from resource_advisor.store import Conflict
from resource_advisor.worker import Worker


def start(service, bundle, policy=True):
    spec, candidate, variant, _ = bundle
    if policy:
        variant = variant.model_copy(
            update={
                "ref": "load-variant",
                "workload_ref": "load-workload",
                "load_context_policy": PROVIDER,
            }
        )
        candidate = candidate.model_copy(update={"variant_ref": variant.ref})
        spec = spec.model_copy(update={"ref": "load-workload", "candidates": (candidate,)})
        for kind, value in [("variant", variant), ("workload", spec)]:
            service.register(kind, value, "team-a")
    job = service.submit(
        "team-a", JobRequest(workload_ref=spec.ref, candidate_ref="base"), "load-trace"
    )
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    worker.submit_one()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
    envelope = backend.result(row)
    before = LoadReading(
        started=0,
        finished=0.001,
        cpu_usage_usec=1000,
        cpu_periods=10,
        cpu_throttled_periods=2,
        cpu_throttled_usec=500,
        memory_current_bytes=1000000,
        observed_process_count=2,
        cpu_quota_usec=100000,
        cpu_period_usec=100000,
        memory_limit_bytes=512 * 1024**2,
    )
    after = before.model_copy(
        update={
            "started": 2,
            "finished": 2.001,
            "cpu_usage_usec": 501000,
            "cpu_periods": 30,
            "cpu_throttled_periods": 12,
            "cpu_throttled_usec": 200500,
            "observed_process_count": 3,
        }
    )
    trace = LoadTrace(
        job_id=row["id"],
        attempt_id=row["body"]["attempt_id"],
        result_digest=envelope["digest"],
        context_signature=row["body"]["context_signature"],
        cgroup_identity_digest=signature("fixture-cgroup"),
        kernel_release="fixture-kernel",
        before=before,
        after=after,
    )
    envelope["load_trace"] = trace.model_dump(mode="json")
    backend.result = lambda _: envelope
    backend.observation = Observation(State.COLLECTING)
    return backend, worker, row, envelope


def test_optional_policy_preserves_legacy_hashes_and_separates_instrumented_context(bundle):
    _, candidate, variant, _ = bundle
    raw = variant.model_dump(mode="json")
    assert "load_context_policy" not in raw
    assert (
        RuntimeVariant.model_validate({**raw, "load_context_policy": None}).model_dump(mode="json")
        == raw
    )
    assert context_signature(candidate, variant) == signature(
        {
            "context": candidate.context.model_dump(mode="json"),
            "image": variant.image,
            "compiled_artifact_digest": variant.compiled_artifact_digest,
            "command": variant.command,
            "pilot_command": variant.pilot_command,
            "backend": candidate.backend,
        }
    )
    assert context_signature(
        candidate, variant.model_copy(update={"load_context_policy": PROVIDER})
    ) != context_signature(candidate, variant)


def test_collector_database_artifact_mlflow_and_project_readback(service, bundle):
    _, worker, row, envelope = start(service, bundle)
    assert identity_environment(row)["RA_LOAD_CONTEXT_POLICY"] == PROVIDER
    worker.reconcile_all()
    assert service.get_job("team-a", row["id"])["state"] == State.SUCCEEDED
    with service.store.transaction() as conn:
        stored = service.store.get(conn, "load_trace", row["body"]["attempt_id"])["body"]
        body = service.store.job(conn, row["id"])["body"]
    assert stored == envelope["load_trace"]
    summary = body["load_context"]
    assert summary["cpu_usage_seconds"] == 0.5
    assert summary["cpu_throttled_period_fraction"] == 0.5
    assert summary["io_read_bytes"] is None and summary["io_pressure_some_seconds"] is None
    assert summary["before_processes"] == 2 and summary["after_processes"] == 3
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=MemoryS3())
    assert ArtifactDelivery(service.store, storage).deliver_one()
    with service.store.transaction() as conn:
        artifact = service.store.list(conn, "artifact", "team-a")[0]["body"]
    payload = json.loads(storage.read(artifact))
    assert payload["load_trace"] == stored and payload["load_context"] == summary
    server = TrackingServer()
    assert delivery(service, server).deliver_one()
    tags = {t["key"]: t["value"] for t in server.batches[0]["tags"]}
    metrics = {m["key"]: m["value"] for m in server.batches[0]["metrics"]}
    assert tags["load_context.trace_digest"] == signature(stored)
    assert metrics["load_context.cpu_usage_seconds"] == 0.5
    assert "load_context.io_read_bytes" not in metrics
    tokens = {hashlib.sha256(k.encode()).hexdigest(): Principal(k) for k in ["team-a", "team-b"]}
    with TestClient(create_app(service, tokens)) as client:
        path = f"/api/v1/compute/jobs/{row['id']}/load-context"
        response = client.get(path, headers={"Authorization": "Bearer team-a"}).json()
        assert response == {"status": "RECORDED", "trace": stored, "summary": summary}
        assert client.get(path, headers={"Authorization": "Bearer team-b"}).status_code == 404
    result = ExecutionResult.model_validate(envelope["result"])
    service.ingest("team-a", result, envelope["digest"], load_trace=stored)
    with pytest.raises(Conflict, match="immutable"):
        service.ingest(
            "team-a", result, envelope["digest"], load_trace={**stored, "kernel_release": "changed"}
        )


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "unbound",
        "owner",
        "context",
        "digest",
        "reset",
        "disappeared",
        "window",
        "cpu",
        "period",
        "memory",
        "nonfinite",
    ],
)
def test_bad_load_evidence_cannot_create_result_profile_or_artifact(service, bundle, fault):
    _, worker, row, envelope = start(service, bundle, policy=fault != "unbound")
    trace = envelope["load_trace"]
    if fault == "missing":
        envelope.pop("load_trace")
    elif fault == "owner":
        trace["attempt_id"] = "foreign-attempt"
    elif fault == "context":
        trace["context_signature"] = signature("other-context")
    elif fault == "digest":
        trace["result_digest"] = signature("other-result")
    elif fault == "reset":
        trace["after"]["cpu_usage_usec"] = 0
    elif fault == "disappeared":
        trace["before"]["io_read_bytes"] = 10
    elif fault == "window":
        trace["after"].update(started=0.4, finished=0.5)
    elif fault == "cpu":
        trace["after"]["cpu_quota_usec"] = 200000
    elif fault == "period":
        trace["after"]["cpu_quota_usec"] *= 2
        trace["after"]["cpu_period_usec"] *= 2
    elif fault == "memory":
        trace["after"]["memory_limit_bytes"] *= 2
    elif fault == "nonfinite":
        trace["after"]["finished"] = float("nan")
    worker.reconcile_all()
    assert service.get_job("team-a", row["id"])["state"] == State.COLLECTING
    with service.store.transaction() as conn:
        for kind in ["result", "profile", "load_trace", "artifact"]:
            assert service.store.get(conn, kind, row["body"]["attempt_id"]) is None


@pytest.fixture
def cgroup_files(tmp_path):
    root, proc = tmp_path / "cgroup", tmp_path / "proc"
    root.mkdir()
    proc.mkdir()
    files = {
        "cpu.stat": "usage_usec 1000\nnr_periods 10\nnr_throttled 2\nthrottled_usec 500\n",
        "cpu.max": "50000 100000\n",
        "memory.max": "1073741824\n",
        "memory.current": "1024000\n",
        "cgroup.procs": f"{os.getpid()}\n{os.getpid()}\n999999\n",
        "cpu.pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=20\n",
        "io.stat": "8:0 rbytes=100 wbytes=200 rios=1 wios=1\n8:1 rbytes=300 wbytes=400 rios=1 wios=1\n",
    }
    for name, value in files.items():
        (root / name).write_text(value)
    (proc / "cgroup").write_text("0::/\n")
    (proc / "mountinfo").write_text(f"1 2 0:9 / {root} ro - cgroup2 cgroup rw\n")
    return root, proc


def test_read_only_provider_aggregates_io_and_preserves_missing_pressure(cgroup_files):
    root, proc = cgroup_files
    reader = CgroupReader(root, proc)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    value = reader.read()
    assert value.cpu_quota_usec / value.cpu_period_usec == 0.5
    assert value.observed_process_count == 2
    assert value.io_read_bytes == 400 and value.io_write_bytes == 600
    assert value.cpu_pressure_some_usec == 20 and value.io_pressure_some_usec is None
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}


@pytest.mark.parametrize("fault", ["membership", "mount", "pid", "cpu", "pressure", "io"])
def test_provider_fails_closed_on_wrong_scope_or_malformed_counters(cgroup_files, fault):
    root, proc = cgroup_files
    if fault == "membership":
        (proc / "cgroup").write_text("0::/host-slice/other-container\n")
    elif fault == "mount":
        (proc / "mountinfo").write_text("1 2 0:9 / /sys/fs/cgroup ro - tmpfs tmpfs rw\n")
    elif fault == "pid":
        (root / "cgroup.procs").write_text("999999\n")
    elif fault == "cpu":
        (root / "cpu.stat").write_text("usage_usec -1\n")
    elif fault == "pressure":
        (root / "cpu.pressure").write_text("some total=nan\n")
    else:
        (root / "io.stat").write_text("8:0 rbytes=-1 wbytes=2\n")
    with pytest.raises(ValueError):
        CgroupReader(root, proc).read()


def test_counter_parsers_do_not_invent_missing_or_negative_values():
    assert io_bytes("") == (0, 0)  # Existing empty io.stat reports no attributed block I/O.
    with pytest.raises(ValueError):
        flat_counters("usage_usec 1\nusage_usec 2\n")
    with pytest.raises(ValueError):
        io_bytes("8:0 rbytes=1 wbytes=2\n8:0 rbytes=3 wbytes=4\n")
    with pytest.raises(ValueError):
        pressure_total("full total=1\n")


def test_zero_period_delta_has_unknown_throttle_fraction(service, bundle):
    _, _, _, envelope = start(service, bundle)
    trace = envelope["load_trace"]
    trace["after"]["cpu_periods"] = trace["before"]["cpu_periods"]
    trace["after"]["cpu_throttled_periods"] = trace["before"]["cpu_throttled_periods"]
    assert summarize(trace)["cpu_throttled_period_fraction"] is None
