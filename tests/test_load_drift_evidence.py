"""Audit retained real GPU observations; no new hardware execution in tests."""

import copy
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
audit = runpy.run_path(str(ROOT / "examples/audit_load_drift.py"))["audit"]


@pytest.fixture
def evidence():
    return json.loads((ROOT / "docs/evidence/load-drift-v1.json").read_text())


def test_real_load_drift_record_recomputes_latching_and_total_cost(evidence):
    result = audit(evidence)
    assert result["independent_gpu_jobs"] == 10
    assert result["gpu_reservation_seconds"] == 197
    assert result["first_drift_slot_zero_based"] == 5
    assert result["drift_remained_latched"] is True
    assert result["competitor_mean_seconds"] > result["baseline_mean_seconds"] * 1.25
    assert result["recovery_mean_seconds"] < result["baseline_mean_seconds"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing_run",
        "duplicate_attempt",
        "counter_reset",
        "helper_wrong_cgroup",
        "thermal_missing_window",
        "revived_recommendation",
        "cost_omitted",
        "storage_disagreement",
        "accepted_old_approval",
        "future_baseline",
        "altered_result",
    ],
)
def test_auditor_rejects_broken_or_contradictory_evidence(evidence, fault):
    report = copy.deepcopy(evidence)
    if fault == "missing_run":
        report["runs"].pop()
    elif fault == "duplicate_attempt":
        report["runs"][1]["result"]["attempt_id"] = report["runs"][0]["result"]["attempt_id"]
    elif fault == "counter_reset":
        report["runs"][0]["load_trace"]["after"]["cpu_usage_usec"] = 0
    elif fault == "helper_wrong_cgroup":
        report["runs"][3]["helper_records"][0]["cgroup_identity_digest"] = "sha256:" + "0" * 64
    elif fault == "thermal_missing_window":
        report["runs"][0]["thermal_trace"]["windows"].pop()
    elif fault == "revived_recommendation":
        report["runs"][-1]["recommendation_validity"].update(reasons=[], reusable=True)
    elif fault == "cost_omitted":
        report["gpu_seconds"] -= report["qualification"]["cost"]["gpu_reservation_seconds"]
    elif fault == "storage_disagreement":
        report["runs"][0]["matching_s3_api_mlflow_bytes"] = False
    elif fault == "accepted_old_approval":
        report["negative_approval"]["status"] = 200
    elif fault == "future_baseline":
        report["recommendation"]["created_at"] = report["completed_at"]
    else:
        report["runs"][0]["result"]["measurements"]["elapsed_seconds"] *= 0.5
    with pytest.raises((AssertionError, ValueError)):
        audit(report)
