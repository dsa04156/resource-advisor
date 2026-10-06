import importlib.util
import json
import subprocess
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "priority_trial", Path(__file__).parents[1] / "examples/verify_slurm_project_isolation.py"
)
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def test_observer_failure_keeps_same_job_without_cancel_or_stale_progress(monkeypatch, tmp_path):
    calls = []
    job = {"job_id": "job-1", "attempt_id": "attempt-1", "external_id": "1", "state": "RUNNING"}

    def request(self, method, path, **kwargs):
        calls.append((method, path))
        body = [] if path == "/jobs" and method == "GET" else job
        return httpx.Response(200, json=body, request=httpx.Request(method, "https://lab" + path))

    monkeypatch.setattr(httpx.Client, "request", request)
    monkeypatch.setattr(trial.ssl, "create_default_context", lambda **kwargs: False)
    ticks = iter([0, 1, 601])
    monkeypatch.setattr(trial.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(trial.time, "sleep", lambda _: None)

    def disconnected(*args, **kwargs):
        raise subprocess.CalledProcessError(255, ["observer"])

    monkeypatch.setattr(trial.subprocess, "check_output", disconnected)
    scope = {
        "token": "test-token",
        "workload_ref": "fixture",
        "profiles": {"normal": "normal", "high": "high"},
    }
    config = {
        "api_url": "https://lab",
        "ca_file": "unused",
        "run_ref": "fixed",
        "observer_command": ["observer"],
        "projects": {"a": scope, "b": scope},
    }
    report = tmp_path / "report.json"
    with pytest.raises(trial.NativeObservationUnavailable):
        trial.verify(config, report)
    saved = json.loads(report.read_text())
    assert saved["status"] == "INCOMPLETE" and saved["rounds"] == []
    assert len(saved["jobs"]) == 1 and len(saved["observer_errors"]) == 1
    assert sum(method == "POST" for method, _ in calls) == 2  # original + idempotent receipt
    assert not any(path.endswith("/cancel") for _, path in calls)
    assert "inspect the same IDs" in saved["stop_reason"]


def test_observer_recovers_same_ids(monkeypatch):
    values = iter([subprocess.TimeoutExpired(["observer"], 40), '{"1": {"native": {}}}'])
    seen = []

    def output(argv, **kwargs):
        seen.append(argv)
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(trial.subprocess, "check_output", output)
    argv = ["observer", "--job", "1"]
    with pytest.raises(trial.NativeObservationUnavailable):
        trial.observe(argv)
    assert trial.observe(argv) == {"1": {"native": {}}}
    assert seen == [argv, argv]
