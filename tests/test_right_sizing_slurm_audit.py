"""Reject mutations of captured hardware evidence, without running workloads."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from audit_right_sizing_slurm import audit  # noqa: E402

from resource_advisor.contracts import signature


@pytest.fixture
def captured():
    root = Path(__file__).resolve().parents[1] / "docs/evidence"
    return (
        json.loads((root / "right-sizing-slurm-feedback-v1.json").read_text()),
        json.loads((root / "right-sizing-slurm-plan-v1.json").read_text()),
    )


def test_native_feedback_and_cost_are_recomputed(captured):
    report, plan = captured
    checked = audit(report, plan)
    assert checked["closed_loop_verified"]
    assert not checked["performance_improvement_claim"]
    assert checked["gpu_reservation_seconds"] == sum(
        row["ledger"]["allocated_device_seconds"] for row in report["attempts"]
    )
    assert checked["next_history_source_count"] > checked["observe_jobs"]


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "native_id",
        "leakage",
        "digest",
        "quality",
        "missing_cost",
        "reservation",
        "budget",
        "reused_confirmation",
        "model",
        "cgroup",
        "publication",
        "next_history",
    ],
)
def test_native_auditor_rejects_invalid_evidence(captured, fault):
    original, plan = captured
    report = copy.deepcopy(original)
    if fault == "duplicate":
        report["attempts"][2]["attempt_id"] = report["attempts"][1]["attempt_id"]
    elif fault == "native_id":
        report["attempts"][2]["native_job_id"] = report["attempts"][1]["native_job_id"]
    elif fault == "leakage":
        report["attempts"][1]["profile"]["recorded_at"] = "2099-01-01T00:00:00+00:00"
    elif fault == "digest":
        report["attempts"][1]["result_digest"] = "sha256:" + "0" * 64
    elif fault == "quality":
        row = report["attempts"][1]
        row["result"]["measurements"]["quality_value"] = plan["workload"]["quality"]["minimum"] - 1
        row["result_digest"] = signature(row["result"])
    elif fault == "missing_cost":
        report["attempts"][1]["ledger"]["allocated_device_seconds"] = None
    elif fault == "reservation":
        report["attempts"][1]["ledger"]["body"]["allocation_memory_mib"] = None
    elif fault == "budget":
        report["attempts"].append(copy.deepcopy(report["attempts"][-1]))
    elif fault == "reused_confirmation":
        row = report["attempts"][-1]
        row["feedback"]["source_profile_refs"].append(row["attempt_id"])
    elif fault == "model":
        report["attempts"][1]["numerical_qualification"]["weights_sha256"] = "0" * 64
    elif fault == "cgroup":
        report["attempts"][1]["numerical_qualification"]["enforced_boundary"]["swap_max_bytes"] = 1
    elif fault == "publication":
        report["attempts"][1]["delivery"]["matching_s3_api_mlflow_bytes"] = False
    else:
        refs = report["next_recommendation"]["ranking"][0]["evidence_refs"]
        refs.remove(report["attempts"][-1]["attempt_id"])
    with pytest.raises(ValueError):
        audit(report, plan)
