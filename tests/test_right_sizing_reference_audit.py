"""Reject inconsistent hardware captures without submitting replacement Jobs."""

import copy
import json
import sys
from pathlib import Path

import pytest

from resource_advisor.contracts import signature

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from audit_right_sizing_reference import audit  # noqa: E402


@pytest.fixture
def captured():
    root = Path(__file__).resolve().parents[1] / "docs/evidence"
    return tuple(
        json.loads((root / name).read_text())
        for name in (
            "right-sizing-gpu-v1.json",
            "right-sizing-trial-plan-v1.json",
            "right-sizing-reference-v3.json",
            "right-sizing-reference-plan-v3.json",
        )
    )


def test_reference_keeps_old_cost_and_new_qualification_separate(captured):
    primary, plan, reference, frozen = captured
    checked = audit(primary, plan, reference, frozen)
    assert checked["fresh_native_jobs"] == len(reference["attempts"]) + len(
        reference["qualifications"]
    )
    assert checked["total_gpu_native_jobs"] == checked["fresh_native_jobs"] + len(
        primary["attempts"]
    ) + len(primary["qualifications"])
    assert {c["workload"] for c in checked["comparison"]} == {
        c["name"] for c in frozen["contracts"]
    }
    assert checked["failures_including_original_reference"] > 0
    assert checked["overall_goal_complete"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "qualification_id",
        "qualification_cost",
        "quality",
        "deadline",
        "startup",
        "clock",
        "variant",
        "censored",
    ],
)
def test_reference_rejects_wrong_provenance_or_missing_cost(captured, fault):
    primary, plan, original, frozen = captured
    reference = copy.deepcopy(original)
    if fault == "qualification_id":
        reference["qualifications"][0]["native"]["job_uid"] = primary["qualifications"][0][
            "native"
        ]["job_uid"]
    elif fault == "qualification_cost":
        reference["qualifications"][0]["device_seconds"] += 1
    elif fault == "quality":
        row = reference["qualifications"][0]
        row["result"]["measurements"]["quality_value"] = (
            frozen["contracts"][0]["workload"]["quality"]["minimum"] - 0.1
        )
        row["result_digest"] = signature(row["result"])
    elif fault == "deadline":
        reference["attempts"][0]["native"]["active_deadline_seconds"] += 1
    elif fault == "startup":
        reference["studies"][0]["study"]["native_startup_assessment"]["source_attempts"][0][
            "startup_upper_observation_seconds"
        ] += 1
    elif fault == "clock":
        reference["protocol_finished_at"] = "2099-01-01T00:00:00+00:00"
    elif fault == "variant":
        reference["attempts"][0]["variant"]["validation_refs"] = ["unqualified"]
    else:
        observation = reference["studies"][0]["study"]["observations"][0]
        observation["measurements"]["elapsed_seconds"] *= 0.1
    with pytest.raises(ValueError):
        audit(primary, plan, reference, frozen)
