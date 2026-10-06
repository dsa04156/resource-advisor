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


def recovery_fixture():
    config = {
        "projects": {
            key: {
                "project_ref": key,
                "workload_ref": "w-" + key,
                "profiles": {"normal": "normal", "high": "high"},
                "token": "test-only",
            }
            for key in ("a", "b")
        },
        "api_url": "https://lab",
        "ca_file": "unused",
        "observer_command": ["observer"],
    }
    original = {"jobs": {}, "intents": {}, "rounds": []}
    i = 0
    for number, holder, other in ((1, "a", "b"), (2, "b", "a")):
        for role, project in (("holder", holder), ("normal", other), ("high", holder)):
            i += 1
            label = f"round-{number}-{role}"
            job = {
                "job_id": "job-" + str(i),
                "attempt_id": "attempt-" + str(i),
                "external_id": str(i),
                "epoch": 1,
                "workload_signature": "w",
                "context_signature": "c",
                "project_ref": project,
                "state": "CANCEL_REQUESTED",
            }
            original["jobs"][label] = {"project": project, "receipt": job, "latest": job}
            original["intents"][label] = {
                "project": project,
                "request": {
                    "workload_ref": "w-" + project,
                    "scheduling_profile_ref": "high" if role == "high" else "normal",
                    "mode": "observe",
                },
            }
    return config, original


def test_read_only_resume_on_observer_loss_preserves_original(monkeypatch, tmp_path):
    config, original = recovery_fixture()
    path = tmp_path / "original.json"
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    requests = []
    by_id = {e["latest"]["job_id"]: e["latest"] for e in original["jobs"].values()}

    def get(self, url):
        requests.append(url)
        return httpx.Response(
            200, json=by_id[url.split("/")[-1]], request=httpx.Request("GET", "https://lab" + url)
        )

    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr(trial.ssl, "create_default_context", lambda **kwargs: False)

    def unavailable(argv):
        assert {argv[i + 1] for i, x in enumerate(argv) if x == "--job"} == {
            str(i) for i in range(1, 7)
        }
        raise trial.NativeObservationUnavailable("TimeoutExpired")

    monkeypatch.setattr(trial, "observe", unavailable)
    result = trial.resume(config, path, tmp_path / "readback.json")
    assert result["status"] == "INCOMPLETE" and len(requests) == 6
    assert result["new_submissions"] == result["cancellations_sent"] == 0
    assert path.read_bytes() == before


def test_resume_rejects_duplicate_native_id_before_requests():
    config, original = recovery_fixture()
    original["jobs"]["round-2-high"]["latest"]["external_id"] = "1"
    with pytest.raises(ValueError, match="duplicate native identity"):
        trial.saved_cohort(config, original)
