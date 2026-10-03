"""Recompute fixed hardware-trial evidence; never launch GPU jobs in tests."""

import json
import runpy
from copy import deepcopy
from pathlib import Path

import pytest

from resource_advisor.contracts import signature

ROOT = Path(__file__).parents[1]
evaluate = runpy.run_path(str(ROOT / "examples/evaluate_e5_kernels.py"))["evaluate"]


@pytest.fixture
def evidence():
    folder = ROOT / "docs/evidence"
    return (
        json.loads((folder / "e5-kernel-v2.json").read_text()),
        json.loads((folder / "e5-kernel-plan-v2.json").read_text()),
    )


def test_complete_trial_report_reproduces(evidence):
    report = evaluate(*evidence)
    stored = json.loads((ROOT / "docs/evidence/e5-kernel-v2-report.json").read_text())
    assert report == stored
    assert report["total_jobs_including_prior_protocol"] == 17
    assert report["GPU_count_changed"] is False


@pytest.mark.parametrize("mutation", ["omit", "reorder", "duplicate", "stopped"])
def test_missing_reordered_or_incomplete_trial_rejected(evidence, mutation):
    capture, plan = evidence
    if mutation == "omit":
        capture["runs"].pop()
    elif mutation == "reorder":
        capture["runs"][0], capture["runs"][1] = capture["runs"][1], capture["runs"][0]
    elif mutation == "duplicate":
        capture["runs"][1] = deepcopy(capture["runs"][0])
    else:
        capture["status"] = "STOPPED"
    with pytest.raises(ValueError):
        evaluate(capture, plan)


@pytest.mark.parametrize("mutation", ["gpu", "quality", "context", "binding"])
def test_incompatible_result_cannot_be_resigned_into_evidence(evidence, mutation):
    capture, plan = evidence
    row = capture["runs"][0]
    if mutation == "gpu":
        row["context"]["resources"]["accelerator_count"] = 0
    elif mutation == "quality":
        row["envelope"]["result"]["measurements"]["quality_value"] = 0.99
        row["envelope"]["digest"] = signature(row["envelope"]["result"])
    elif mutation == "binding":
        row["context_binding"]["command"] = ["python", "different.py"]
        row["envelope"]["result"]["context_signature"] = signature(row["context_binding"])
        row["envelope"]["digest"] = signature(row["envelope"]["result"])
    else:
        row["context"]["parameters"]["input_strategy"] = "recompute"
    with pytest.raises(ValueError):
        evaluate(capture, plan)


@pytest.mark.parametrize("mutation", ["ordinary_profile", "kernel_count", "cpu_fallback"])
def test_profiler_evidence_cannot_be_mixed_or_faked(evidence, mutation):
    capture, plan = evidence
    row = next(r for r in capture["runs"] if r["arm"] == "cache-profiler")
    if mutation == "ordinary_profile":
        row["ordinary_profile_created"] = True
    elif mutation == "kernel_count":
        row["envelope"]["kernel_diagnostics"]["kernel_count"] += 1
    else:
        row["trace_input"]["traceEvents"] = [
            e for e in row["trace_input"]["traceEvents"] if e["cat"] != "kernel"
        ]
    with pytest.raises(ValueError):
        evaluate(capture, plan)


def test_missing_allocation_is_not_zero_cost(evidence):
    capture, plan = evidence
    capture["runs"][0]["cost"]["gpu_reservation_seconds"] = 0
    with pytest.raises(ValueError, match="allocation"):
        evaluate(capture, plan)


def test_missing_delivery_verification_rejected(evidence):
    capture, plan = evidence
    capture["runs"][0]["verification"]["matching_s3_api_mlflow_bytes"] = False
    with pytest.raises(ValueError, match="delivery"):
        evaluate(capture, plan)


def test_failed_predecessor_cost_cannot_disappear(evidence):
    capture, plan = evidence
    capture["prior_protocol"]["gpu_reservation_seconds"] = 0
    with pytest.raises(ValueError, match="predecessor costs"):
        evaluate(capture, plan)
