import copy
import hashlib
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from resource_advisor.api import Principal, create_app
from resource_advisor.contracts import JobRequest, now, signature
from resource_advisor.passive import PassiveImport, PassiveObservations
from resource_advisor.passive_collector import (
    PassiveNvml,
    ProcReader,
    TargetChanged,
    collect,
    process_stat,
)
from resource_advisor.store import entities, jobs, outbox, usage


def payload():
    at = now() - timedelta(minutes=1)
    return {
        "ref": "observation-1",
        "project_ref": "team-a",
        "evidence_kind": "synthetic",
        "collector_digest": signature("test-only"),
        "target": {
            "backend": "kubernetes",
            "backend_cluster_id": "lab",
            "node_ref": "test-worker",
            "external_job_ref": "external-job",
            "external_job_uid": "immutable-uid",
            "attempt_ref": "external-attempt",
            "process_identity_digest": signature("test-process"),
        },
        "started_at": at.isoformat(),
        "finished_at": (at + timedelta(seconds=3)).isoformat(),
        "requested_window_seconds": 3,
        "interval_seconds": 1,
        "stop_reason": "WINDOW_COMPLETE",
        "samples": [
            {
                "started_seconds": i,
                "finished_seconds": i + 0.01,
                "process": {
                    "cpu_seconds": i / 2,
                    "rss_bytes": (32 + i) * 1024**2,
                    "read_bytes": i * 100,
                    "write_bytes": None,
                    "errors": {"write_bytes": "PERMISSION_DENIED"},
                },
                "node": {
                    "cpu_total_ticks": 100 + i,
                    "cpu_idle_ticks": 50,
                    "memory_total_bytes": 1024**3,
                    "memory_available_bytes": 512 * 1024**2,
                },
            }
            for i in range(3)
        ],
    }


def api(service):
    credentials = {
        hashlib.sha256(token.encode()).hexdigest(): Principal(project, operator)
        for token, project, operator in (
            ("operator", "team-a", True),
            ("reader", "team-a", False),
            ("foreign", "team-b", True),
        )
    }
    return TestClient(create_app(service, credentials))


def headers(token="operator"):
    return {"Authorization": "Bearer " + token}


def counts(service):
    with service.store.transaction() as conn:
        return [
            conn.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (entities, jobs, outbox, usage)
        ]


def test_operator_import_scope_idempotency_and_no_execution_authority(service):
    before = counts(service)
    report = payload()
    url = "/api/v1/compute/observations"
    with api(service) as client:
        assert client.post(url, json=report).status_code == 401
        assert client.post(url, json=report, headers=headers("reader")).status_code == 403
        assert client.post(url, json=report, headers=headers("foreign")).status_code == 422
        response = client.post(url, json=report, headers=headers())
        assert response.status_code == 200
        value = response.json()
        a = value["assessment"]
        assert a["process_cpu_seconds"] == 1
        assert a["process_sampled_peak_rss_mib"] == 34
        assert a["process_read_bytes"] == 200 and a["process_write_bytes"] is None
        assert a["throughput"] is None and a["quality_value"] is None
        assert a["recommendation_authorized"] is False
        assert client.post(url, json=report, headers=headers()).json() == value
        read = client.get(url + "/observation-1", headers=headers("reader"))
        assert read.status_code == 200 and len(read.json()["samples"]) == 3
        assert read.headers["cache-control"] == "no-store"
        assert client.get(url + "/observation-1", headers=headers("foreign")).status_code == 404
        assert client.get(url, headers=headers("foreign")).json()["total"] == 0
        overview = client.get("/api/v1/compute/overview", headers=headers("reader")).json()
        assert overview["observations"]["items"][0] == value
        assert overview["observations"]["size"] == 12
        assert overview["observations"]["has_next"] is False
        report["samples"][0]["process"]["rss_bytes"] += 1
        assert client.post(url, json=report, headers=headers()).status_code == 409
    assert counts(service) == [before[0] + 1, *before[1:]]


@pytest.mark.parametrize("mutation", ["quality", "counter", "overlap", "missing", "slurm"])
def test_missing_metrics_and_scope_cannot_be_promoted(mutation):
    report = payload()
    if mutation == "quality":
        report["quality_value"] = 1
    elif mutation == "counter":
        report["samples"][1]["process"]["cpu_seconds"] = 2
    elif mutation == "overlap":
        report["samples"][1]["started_seconds"] = 0
    elif mutation == "missing":
        report["samples"][0]["process"]["errors"] = {}
    else:
        report["target"]["backend"] = "slurm"
    with pytest.raises(ValidationError):
        PassiveImport.model_validate(report)


def test_platform_binding_requires_exact_attempt_and_native_identity(service):
    row = service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), "one"
    )
    with service.store.transaction() as conn:
        row = service.store.job(conn, row["job_id"])
        service.store.change_job(
            conn,
            row,
            "RUNNING",
            dict(row["body"], external_id="external-job", backend_uid="immutable-uid"),
        )
    report = payload()
    report["target"].update(platform_job_id=row["id"], attempt_ref=row["body"]["attempt_id"])
    observations = PassiveObservations(service.store, accept_synthetic=True)
    value = observations.create("team-a", PassiveImport.model_validate(report))
    assert value["binding_assurance"].startswith("platform execution metadata matched")
    bad = copy.deepcopy(report)
    bad["ref"] = "wrong-uid"
    bad["target"]["external_job_uid"] = "another-uid"
    with pytest.raises(ValueError, match="persisted execution identity"):
        observations.create("team-a", PassiveImport.model_validate(bad))


def stat_text(start=123):
    fields = ["0"] * 22
    fields[0], fields[11], fields[12], fields[19], fields[21] = "R", "10", "5", str(start), "2"
    return "987 (name (including) spaces) " + " ".join(fields)


def test_process_identity_refuses_pid_reuse(tmp_path):
    proc = tmp_path / "987"
    (proc / "ns").mkdir(parents=True)
    (proc / "ns/pid").write_text("namespace")
    (proc / "stat").write_text(stat_text())
    boot = tmp_path / "sys/kernel/random"
    boot.mkdir(parents=True)
    (boot / "boot_id").write_text("test-boot")
    reader = ProcReader(987, root=tmp_path)
    assert process_stat(stat_text())["cpu_ticks"] == 15
    sample = reader.read()
    assert (
        sample.process.read_bytes is None and sample.process.errors["read_bytes"] == "UNAVAILABLE"
    )
    (proc / "stat").write_text(stat_text(start=124))
    with pytest.raises(TargetChanged):
        reader.read()


def test_slurm_identity_and_counter_gap_remain_explicit():
    report = payload()
    report["target"].update(
        backend="slurm", external_job_uid=None, native_submitted_at=report["started_at"]
    )
    assert PassiveImport.model_validate(report).target.native_submitted_at is not None
    report["samples"][0]["process"]["cpu_seconds"] = 3
    report["samples"][1]["process"]["cpu_seconds"] = None
    report["samples"][1]["process"]["errors"]["cpu_seconds"] = "UNAVAILABLE"
    with pytest.raises(ValidationError, match="counter reset"):
        PassiveImport.model_validate(report)


def test_unsupported_nvml_sensors_are_missing_not_zero():
    reader = SimpleNamespace(
        device_uuid_digest=signature("test-device"),
        handle=None,
        read=lambda: SimpleNamespace(
            temperature_c=None, power_w=None, errors={"temperature_c": 3, "power_w": 3}
        ),
        _call=lambda *args: 3,
    )
    device = PassiveNvml("unused-fixture-uuid", reader=reader).read()
    assert device.utilization_percent is None and device.memory_used_bytes is None
    assert device.scope == "physical-device-inclusive"
    assert len(device.errors) == 4


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux proc reader")
def test_actual_unmodified_program_survives_collection_and_has_no_model_metrics(tmp_path):
    # This is an actual ordinary process, not a synthetic performance result.
    code = "import time; time.sleep(1.5); print('original-output', flush=True)"
    program = tmp_path / "research.py"
    program.write_text(code)
    digest = hashlib.sha256(program.read_bytes()).hexdigest()
    process = subprocess.Popen([sys.executable, str(program)], stdout=subprocess.PIPE, text=True)
    try:
        binding = {
            key: value
            for key, value in payload().items()
            if key in {"ref", "project_ref", "target"}
        }
        report = collect(ProcReader(process.pid), binding, duration=0.35, interval=0.1)
        assert len(report.samples) >= 2 and report.evidence_kind == "hardware"
        assert process.poll() is None
        assert report.quality_value is None and report.throughput is None
        assert report.stop_reason == "WINDOW_COMPLETE"
        assert hashlib.sha256(program.read_bytes()).hexdigest() == digest
        output, _ = process.communicate(timeout=5)
        assert process.returncode == 0 and output.strip() == "original-output"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
