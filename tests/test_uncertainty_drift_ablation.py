"""Retained-data replay and adversarial audit checks; no hardware work."""

import copy
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def evaluator(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "examples"))
    return runpy.run_path(str(ROOT / "examples/evaluate_uncertainty_drift_ablation.py"))


@pytest.fixture
def capture():
    return json.loads((ROOT / "docs/evidence/load-drift-v1.json").read_text())


def test_real_chronological_masks_do_not_use_the_current_target(evaluator, capture):
    before = copy.deepcopy(capture)
    result = evaluator["evaluate"](capture)
    assert [r["strict_reuse"] for r in result["decisions"]] == [True] * 3 + [False] * 3
    assert all(r["without_drift_reuse"] for r in result["decisions"])
    for row in result["decisions"]:
        assert row["target_attempt_id"] not in row["available_followup_attempt_ids"]
    assert result["decisions"][2]["strict_reuse"] is True
    assert result["rates"]["strict"]["forecast_tolerance_exceedance_count"] == 3
    assert result["rates"]["without_drift"]["forecast_tolerance_exceedance_count"] == 3
    assert result["new_gpu_jobs"] == 0
    assert result["original_gpu_reservation_seconds_not_saved"] == 197
    assert capture == before


def test_only_drift_reason_is_removed_other_reasons_still_abstain(evaluator, capture):
    # Synthetic policy-field perturbation, not additional hardware evidence.
    capture["runs"][5]["recommendation_validity"]["reasons"].append(
        "CURRENT_QUALIFICATION_UNAVAILABLE"
    )
    result = evaluator["evaluate"](capture)
    assert result["decisions"][3]["without_drift_reuse"] is False
    assert result["decisions"][3]["without_drift_reasons"] == ["CURRENT_QUALIFICATION_UNAVAILABLE"]


@pytest.mark.parametrize(
    "fault",
    [
        "future_assessment",
        "current_target_leak",
        "foreign_policy",
        "foreign_rec",
        "support",
        "interval",
        "quality",
        "memory",
        "cost",
    ],
)
def test_rejects_leakage_or_invalid_evidence(evaluator, capture, fault):
    validity = capture["runs"][3]["recommendation_validity"]
    if fault == "future_assessment":
        validity["assessed_at"] = capture["completed_at"]
    elif fault == "current_target_leak":
        validity["residuals"].append(
            copy.deepcopy(capture["runs"][4]["recommendation_validity"]["residuals"][-1])
        )
    elif fault == "foreign_policy":
        validity["policy_version"] = "unknown"
    elif fault == "foreign_rec":
        validity["recommendation_ref"] = "another-rec"
    elif fault == "support":
        capture["recommendation"]["evidence_refs"][-1] = capture["runs"][3]["result"]["attempt_id"]
    elif fault == "interval":
        capture["recommendation"]["interval_seconds"][1] *= 2
    elif fault == "quality":
        capture["runs"][4]["result"]["measurements"]["quality_value"] = 0.5
    elif fault == "memory":
        capture["runs"][4]["result"]["measurements"]["peak_memory_mib"] = 4096
    else:
        capture["gpu_seconds"] -= 1
    with pytest.raises((AssertionError, ValueError)):
        evaluator["evaluate"](capture)


def test_frozen_capture_rejects_changed_plan_or_bytes(evaluator, tmp_path):
    source = ROOT / "docs/evidence/load-drift-v1.json"
    plan = json.loads((ROOT / "docs/evidence/uncertainty-drift-ablation-plan-v1.json").read_text())
    assert evaluator["evaluate_capture"](source, plan)["changed_decisions"] == 3
    changed = tmp_path / "capture.json"
    changed.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="bytes changed"):
        evaluator["evaluate_capture"](changed, plan)
    plan["relative_error_limit"] = 0.5
    with pytest.raises(ValueError, match="scope changed"):
        evaluator["evaluate_capture"](source, plan)
