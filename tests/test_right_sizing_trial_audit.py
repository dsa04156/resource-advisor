"""Mutate captured evidence to test auditor rejection, without running benchmarks."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from audit_right_sizing_trial import audit_hailo_feedback  # noqa: E402


@pytest.fixture
def evidence():
    root = Path(__file__).resolve().parents[1] / "docs/evidence"
    return (
        json.loads((root / "right-sizing-hailo-v1.json").read_text()),
        json.loads((root / "hailo-resnet50-inputs.json").read_text()),
    )


def test_captured_hailo_feedback_recomputes(evidence):
    report, manifest = evidence
    audited = audit_hailo_feedback(report, manifest)
    assert audited["feedback_status"] == "COMPARABLE"
    assert (
        audited["total_npu_reservation_seconds"]
        == sum(r["ledger"]["allocated_device_seconds"] for r in report["runs"])
        + report["qualification"]["scheduled_to_container_finished_seconds"]
    )


@pytest.mark.parametrize("fault", ["digest", "cost", "duplicate", "future", "residual", "reuse"])
def test_hailo_auditor_rejects_corrupted_capture(evidence, fault):
    original, manifest = evidence
    report = copy.deepcopy(original)
    if fault == "digest":
        report["runs"][0]["result"]["context_signature"] = "sha256:" + "0" * 64
    elif fault == "cost":
        report["runs"][0]["ledger"]["allocated_device_seconds"] = None
    elif fault == "duplicate":
        report["runs"][1]["backend"]["uid"] = report["runs"][0]["backend"]["uid"]
    elif fault == "future":
        report["runs"][0]["profile"]["recorded_at"] = "2099-01-01T00:00:00+00:00"
    elif fault == "residual":
        report["runs"][3]["feedback"]["residual_seconds"] += 1
    else:
        report["runs"][3]["feedback"]["source_profile_refs"].append(
            report["runs"][3]["result"]["attempt_id"]
        )
    with pytest.raises(ValueError):
        audit_hailo_feedback(report, manifest)
