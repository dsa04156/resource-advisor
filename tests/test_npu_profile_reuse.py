"""Essential boundary checks for existing-model NPU experiments."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from run_npu_profile_reuse import choose_candidate, hailo_routes, native_timing, qualify_result

from resource_advisor.contracts import signature


def test_failed_native_result_never_becomes_a_reusable_profile():
    log = "RESOURCE_ADVISOR_RESULT " + json.dumps(
        {"result": {"outcome": "FAILED"}, "digest": "invalid"}
    )
    with pytest.raises(ValueError, match="completed"):
        qualify_result(log, {})


def test_compiled_model_input_and_runtime_must_match_the_frozen_candidate():
    candidate = {
        "group": "mobilint",
        "model_digest": "model",
        "input_digest": "input",
        "work_units": 10,
        "workload_signature": "work",
        "context_signature": "context",
        "runtime_versions": {"qbruntime": "1.4.0"},
        "runner_digest": "runner",
    }
    report = {
        "evidence_kind": "hardware",
        "model_digest": "model",
        "input_digest": "foreign",
        "work_units": 10,
        "runtime_versions": {"qbruntime": "1.4.0"},
        "runner_digest": "runner",
        "samples_seconds": [0.1] * 10,
        "elapsed_seconds": 1.0,
        "quality_value": 1.0,
        "output_digests": ["ref"] * 10,
        "reference_output_digest": "sha256:ref",
    }
    result = {
        "outcome": "COMPLETED",
        "evidence_kind": "hardware",
        "workload_signature": "work",
        "context_signature": "context",
        "measurements": {"work_units": 10, "elapsed_seconds": 1.0, "quality_value": 1.0},
    }
    envelope = "RESOURCE_ADVISOR_RESULT " + json.dumps(
        {"result": result, "digest": signature(result)}
    )

    def log():
        return "RESOURCE_ADVISOR_NPU_REPORT " + json.dumps(report) + "\n" + envelope

    with pytest.raises(ValueError, match="input"):
        qualify_result(log(), candidate)
    report["input_digest"] = "input"
    assert qualify_result(log(), candidate)["elapsed_seconds"] == 1.0


def test_profile_reuse_uses_frozen_cost_and_cohort_backlog():
    profiles = {"fast": {"service_seconds": 2.0}, "slow": {"service_seconds": 5.0}}
    assert (
        choose_candidate("profile_reuse", ["fast", "slow"], profiles, {"fast": 4.0, "slow": 0.0}, 0)
        == "slow"
    )
    assert choose_candidate("round_robin", ["fast", "slow"], profiles, {}, 1) == "slow"


def test_unrelated_pod_or_cpu_route_cannot_supply_native_success():
    job = {"metadata": {"uid": "owned", "creationTimestamp": "2026-10-08T00:00:00Z"}}
    pod = {"metadata": {"ownerReferences": [{"kind": "Job", "uid": "foreign"}]}}
    with pytest.raises(ValueError, match="owner"):
        native_timing(job, [pod], {"node": "npu-node"})


def test_existing_capture_never_replays_native_submissions(tmp_path):
    from run_npu_profile_reuse import run

    capture = tmp_path / "capture.json"
    capture.write_text('{"status":"STOPPED"}')
    with pytest.raises(ValueError, match="never duplicate"):
        run(tmp_path, tmp_path / "nonexistent")
    assert capture.read_text() == '{"status":"STOPPED"}'


def test_reservation_includes_pre_container_device_allocation():
    candidate = {"node": "bound-npu-node", "image": "pinned-image", "resource_key": "npu"}
    job = {
        "metadata": {"uid": "owned", "creationTimestamp": "2026-10-08T00:00:00Z"},
        "status": {"succeeded": 1},
    }
    pod = {
        "metadata": {"uid": "pod", "ownerReferences": [{"kind": "Job", "uid": "owned"}]},
        "spec": {
            "nodeName": "bound-npu-node",
            "containers": [
                {
                    "image": "pinned-image",
                    "resources": {"requests": {"npu": "1"}},
                }
            ],
        },
        "status": {
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "True",
                    "lastTransitionTime": "2026-10-08T00:00:01Z",
                }
            ],
            "containerStatuses": [
                {
                    "imageID": "pinned-image",
                    "restartCount": 0,
                    "state": {
                        "terminated": {
                            "exitCode": 0,
                            "startedAt": "2026-10-08T00:00:03Z",
                            "finishedAt": "2026-10-08T00:00:06Z",
                        }
                    },
                }
            ],
        },
    }
    timing = native_timing(job, [pod], candidate)
    assert timing["accelerator_reservation_seconds"] == 5.0
    assert timing["container_running_seconds"] == 3.0
    pod["status"]["conditions"] = []
    with pytest.raises(ValueError, match="PodScheduled"):
        native_timing(job, [pod], candidate)


def test_hailo_routes_follow_private_capabilities_and_templates():
    contract = {
        "workload": {"candidates": [{"capability_ref": "cap-a"}, {"capability_ref": "cap-b"}]},
        "capabilities": [
            {"ref": "cap-a", "node_ref": "configured-node-a"},
            {"ref": "cap-b", "node_ref": "configured-node-b"},
        ],
    }

    def template(node, queue):
        return {
            "metadata": {"labels": {"kueue.x-k8s.io/queue-name": queue}},
            "spec": {"template": {"spec": {"nodeSelector": {"kubernetes.io/hostname": node}}}},
        }

    routes = hailo_routes(
        contract,
        [template("configured-node-b", "queue-b"), template("configured-node-a", "queue-a")],
    )
    assert [(r["node"], r["queue"]) for r in routes] == [
        ("configured-node-a", "queue-a"),
        ("configured-node-b", "queue-b"),
    ]


def test_saved_receipt_correction_preserves_original_inputs_and_metrics(tmp_path):
    from run_npu_profile_reuse import correct_metrics

    candidate = {"ref": "device", "node": "bound-node", "image": "pinned", "resource_key": "npu"}

    def save(name, value):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def row(name):
        job = {
            "metadata": {"uid": name, "creationTimestamp": "2026-10-08T00:00:00Z"},
            "status": {"succeeded": 1},
        }
        pod = {
            "metadata": {"uid": "pod-" + name, "ownerReferences": [{"kind": "Job", "uid": name}]},
            "spec": {
                "nodeName": "bound-node",
                "containers": [{"image": "pinned", "resources": {"requests": {"npu": "1"}}}],
            },
            "status": {
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "True",
                        "lastTransitionTime": "2026-10-08T00:00:01Z",
                    }
                ],
                "containerStatuses": [
                    {
                        "imageID": "pinned",
                        "state": {
                            "terminated": {
                                "exitCode": 0,
                                "startedAt": "2026-10-08T00:00:03Z",
                                "finishedAt": "2026-10-08T00:00:06Z",
                            }
                        },
                    }
                ],
            },
        }
        save(f"jobs/{name}/job.json", job)
        save(f"jobs/{name}/pods.json", {"items": [pod]})
        save(f"jobs/{name}/submit-intent.json", {"name": name})
        save(f"jobs/{name}/manifest.json", {"name": name})
        return {
            "name": name,
            "candidate_ref": "device",
            "group": "mobilint",
            "qualified": True,
            "timing": {"native_jct_seconds": 6.0, "accelerator_reservation_seconds": 3.0},
            "report": {"elapsed_seconds": 1.0, "quality_value": 1.0},
        }

    capture = {
        "profiles": [row("profile")],
        "cohorts": [{"group": "mobilint", "arm": "round_robin", "jobs": [row("main")]}],
    }
    save("capture.json", capture)
    save("plan.json", {"candidates": [candidate]})
    save("frozen-profiles.json", {"profiles": {"device": {"service_seconds": 6.0}}})
    save(
        "summary.json",
        {
            "status": "COMPLETED",
            "limitations": "original",
            "cohorts": [
                {
                    "group": "mobilint",
                    "arm": "round_robin",
                    "reservation_seconds_sum": 3.0,
                    "mean_jct_seconds": 6.0,
                    "compute_seconds_sum": 1.0,
                }
            ],
        },
    )
    save("recording-scope.json", {"reservation_scope": "old container interval"})
    save("checksums.json", {"old": "hash"})
    (tmp_path / "rows.csv").write_text("original rows\n")
    originals = {
        name: (tmp_path / name).read_bytes()
        for name in (
            "capture.json",
            "plan.json",
            "frozen-profiles.json",
            "summary.json",
            "rows.csv",
            "checksums.json",
        )
    }
    summary = correct_metrics(tmp_path)
    assert summary["cohorts"][0]["reservation_seconds_sum"] == 5.0
    assert summary["cohorts"][0]["container_running_seconds_sum"] == 3.0
    assert summary["cohorts"][0]["mean_jct_seconds"] == 6.0
    for name in ("capture.json", "plan.json", "frozen-profiles.json", "summary.json", "rows.csv"):
        assert (tmp_path / name).read_bytes() == originals[name]
    for name in ("summary.json", "rows.csv", "checksums.json"):
        assert (tmp_path / "pre-review-metric-output" / name).read_bytes() == originals[name]
