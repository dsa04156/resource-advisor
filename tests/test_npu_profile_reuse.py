"""Essential boundary checks for existing-model NPU experiments."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from run_npu_profile_reuse import choose_candidate, native_timing, qualify_result

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
