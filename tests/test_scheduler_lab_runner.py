import json
import runpy
from pathlib import Path

import pytest

Agent = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "examples/scheduler_lab/agent.py")
)["Agent"]


def runner():
    a = Agent.__new__(Agent)
    a.c = json.loads(Path("examples/scheduler_lab/config.example.json").read_text())
    a.ref = "lab-owned-test"
    a.snapshot = {}
    return a


def test_priority_and_failure_are_fixed_native_job_options():
    a = runner()
    applied = []
    a.kube = lambda *args, value=None: applied.append(value)
    a.job("failure", 1, 1, priority="hairp-lab-low", fail=True)
    job = applied[-1]
    assert job["metadata"]["labels"]["kueue.x-k8s.io/priority-class"] == "hairp-lab-low"
    assert job["spec"]["suspend"] is True
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert "sys.exit(42)" in container["command"][-1]
    assert job["spec"]["backoffLimit"] == 0


@pytest.mark.parametrize(
    "phase,condition,expected",
    [
        ("Pending", None, "ADMITTED"),
        ("Running", None, "RUNNING"),
        ("Succeeded", None, "FINALIZING"),
        ("Succeeded", "Complete", "SUCCEEDED"),
        ("Failed", "Failed", "FAILED"),
    ],
)
def test_native_lifecycle_and_retired_job_survives(phase, condition, expected):
    a = runner()
    name = a.ref + "-new"
    a.snapshot["retired_jobs"] = [{"id": "old", "label": "old", "state": "CANCELED"}]
    fixtures = {
        "jobs": [{"metadata": {"name": name}, "spec": {"parallelism": 1}, "status": {"active": 1}}],
        "pods": [
            {
                "metadata": {"name": "new-pod", "labels": {"job-name": name}},
                "spec": {"nodeName": "node-a"},
                "status": {"phase": phase},
            }
        ],
        "workloads.kueue.x-k8s.io": [
            {
                "metadata": {"ownerReferences": [{"name": name}]},
                "spec": {"priority": 100},
                "status": {
                    "conditions": [{"type": "Admitted", "status": "True"}],
                    "admission": {"clusterQueue": "lab"},
                },
            }
        ],
    }
    if condition:
        fixtures["jobs"][0]["status"]["conditions"] = [{"type": condition, "status": "True"}]
    a.kube = lambda command, resource, *args: (
        "" if command == "logs" else json.dumps({"items": fixtures[resource]})
    )
    result = a.observe_kube()
    assert result["new"]["state"] == expected
    assert result["new"]["priority"] == 100
    assert result["old"]["state"] == "CANCELED"


def test_recovery_allowlist_does_not_hide_unrelated_failure():
    a = runner()
    a.tick = lambda title: None
    a.observe_kube = lambda: {"expected-failure": {"label": "expected-failure", "state": "FAILED"}}
    assert a.wait_kube(lambda r: True, "observed", {"expected-failure"})
    a.observe_kube = lambda: {"recovered": {"label": "recovered", "state": "FAILED"}}
    with pytest.raises(RuntimeError, match="probe job failed"):
        a.wait_kube(lambda r: True, "observed", {"expected-failure"})


def test_backlog_accepts_native_head_reason_without_fabricating_other_reasons():
    a = runner()
    labels = ["request-a", "request-b", "request-c"]
    queued = {
        label: {
            "label": label,
            "state": "PENDING",
            "reason": "insufficient quota" if i == 0 else None,
        }
        for i, label in enumerate(labels)
    }
    observations = iter([queued, {label: {"state": "SUCCEEDED"} for label in labels}])
    a.occupy_pool = lambda *args: None
    a.job = lambda *args: None
    a.observe_kube = lambda: queued
    a.tick = lambda *args: None
    a.retire = lambda *args: None

    def wait(predicate, title):
        result = next(observations)
        assert predicate(result)
        return result

    a.wait_kube = wait
    a.quota()
    assert a.snapshot["queue_evidence"][1]["reason"] is None
    assert "verdict" in a.snapshot


def test_burst_submits_ten_concurrently_and_requires_native_queue_and_results():
    import threading

    a = runner()
    barrier = threading.Barrier(10, timeout=5)
    calls = []

    def submit(label, count, duration, **options):
        calls.append((label, count, duration))
        barrier.wait()  # Sequential submission would fail this check.

    a.job = submit
    a.report = lambda *args: None
    labels = [f"request-{index:02d}" for index in range(1, 11)]
    queued = {
        label: {"state": "RUNNING" if i < 2 else "PENDING", "reason": "quota" if i == 2 else None}
        for i, label in enumerate(labels)
    }
    done = {
        label: {
            "state": "SUCCEEDED",
            "gpu": 1,
            "pods": [{"events": [{"phase": "COMPUTE_FINISHED", "correctness": True}]}],
        }
        for label in labels
    }
    waits = []

    def wait(predicate, title):
        waits.append(predicate)
        result = queued if len(waits) == 1 else done
        assert predicate(result)
        return result

    a.wait_kube = wait
    a.burst()
    assert len(calls) == 10 and {c[0] for c in calls} == set(labels)
    assert all(c[1:] == (1, 12) for c in calls)
    assert len(a.snapshot["queue_evidence"]) == 10
    assert not waits[0]({label: {"state": "RUNNING"} for label in labels})
    done[labels[-1]]["pods"] = []
    assert not waits[1](done)  # Native success alone is insufficient numerical evidence.


def test_registered_batch_recovers_lost_receipt_with_same_key_and_keeps_ten_rows(monkeypatch):
    import threading

    a = runner()
    a.report = lambda *args: None
    a.tick = lambda *args: None
    monkeypatch.setattr("time.sleep", lambda _: None)
    lock = threading.Lock()
    accepted = {}
    retries = []
    sizes = []
    observed_during_acceptance = []

    def api(path, body=None, key=None):
        if path == "/jobs":
            with lock:
                if key not in accepted:
                    accepted[key] = {"job_id": key, "state": "RECEIVED"}
                    raise TimeoutError("server accepted; response was lost")
                retries.append(key)
                return accepted[key]
        return {"state": "SUCCEEDED", "backend": "kubernetes", "result": {"valid": True}}

    a.api = api

    def report(*args):
        sizes.append(len(a.snapshot["jobs"]))
        observed_during_acceptance.append(
            any(j["state"] == "SUCCEEDED" for j in a.snapshot["jobs"])
        )

    a.report = report
    tasks = [
        {"name": f"request-{i}", "device_class": "gpu", "workload_ref": "w", "profile_ref": "p"}
        for i in range(10)
    ]
    a.registered_workloads(tasks, "ten")
    assert len(accepted) == 10 and set(retries) == set(accepted)
    assert sizes and all(size == 10 for size in sizes)
    assert any(observed_during_acceptance[:-1])
    assert len(a.snapshot["platform_jobs"]) == 10
    assert [j["label"] for j in a.snapshot["jobs"]] == [t["name"] for t in tasks]
    assert all(j["state"] == "SUCCEEDED" for j in a.snapshot["jobs"])


@pytest.mark.parametrize("failure", ["tls", "timeout", "client"])
def test_agent_observation_rpc_retries_transient_disconnect_only(tmp_path, monkeypatch, failure):
    import io
    import ssl
    import urllib.error
    import urllib.request

    a = runner()
    a.context = ssl.create_default_context()
    cred = tmp_path / "auth.json"
    cred.write_text(json.dumps({"api_url": "https://lab.invalid", "operator_token": "test-only"}))
    a.c["credentials_file"] = str(cred)
    calls = []
    monkeypatch.setattr("time.sleep", lambda _: None)

    def open_response(request, **kwargs):
        calls.append(request.full_url)
        if len(calls) == 1:
            if failure == "client":
                raise urllib.error.HTTPError(request.full_url, 403, "denied", {}, None)
            if failure == "tls":
                raise urllib.error.URLError(ssl.SSLEOFError("temporary handshake EOF"))
            raise TimeoutError("read timeout")
        return io.BytesIO(b'{"items":[]}')

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    if failure == "client":
        with pytest.raises(urllib.error.HTTPError):
            a.api("/scheduler-labs")
        assert len(calls) == 1
    else:
        assert a.api("/scheduler-labs") == {"items": []}
        assert len(calls) == 2 and calls[0] == calls[1]


def test_lost_final_report_preserves_terminal_receipt_without_reexecuting():
    import urllib.error

    a = runner()
    executions = []
    a.fleet_batch = lambda: (executions.append("one"), a.snapshot.update(verdict="completed"))
    a.cleanup = lambda: None

    def report(title=None, state="RUNNING"):
        if state == "SUCCEEDED":
            raise urllib.error.URLError("lost final acknowledgement")

    a.report = report
    with pytest.raises(urllib.error.URLError):
        a.execute({"ref": a.ref, "scenario": "fleet_batch", "state": "REQUESTED", "body": {}})
    assert a.pending_report == ("completed", "SUCCEEDED")
    assert a.snapshot["cleanup"] == "completed"
    receipts = []
    a.report = lambda *args: receipts.append(args)
    a.report(*a.pending_report)
    assert receipts == [("completed", "SUCCEEDED")] and executions == ["one"]
