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
