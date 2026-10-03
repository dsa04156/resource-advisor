"""Synthetic replay contracts only; these are not hardware performance data."""

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

import pytest

examples = Path(__file__).parents[1] / "examples"
spec = importlib.util.spec_from_file_location(
    "evaluate_policy_comparison", examples / "evaluate_policy_comparison.py"
)
audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)
spec = importlib.util.spec_from_file_location(
    "evaluate_uncertainty_ablation", examples / "evaluate_uncertainty_ablation.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def study():
    observations = []
    for ref, values in (("baseline", [9, 10, 11]), ("candidate", [8.9, 9.9, 10.9])):
        for i, value in enumerate(values):
            observations.append(
                {
                    "attempt_id": f"{ref}-{i}",
                    "candidate_ref": ref,
                    "mode": "confirmation",
                    "outcome": "COMPLETED",
                    "recorded_at": "2026-01-01T00:00:10+00:00",
                    "measurements": {
                        "elapsed_seconds": value,
                        "quality_value": 1,
                        "peak_memory_mib": 100,
                    },
                }
            )
    return {
        "created_at": "2026-01-01T00:00:00+00:00",
        "spec": {
            "baseline_candidate_ref": "baseline",
            "candidates": [{"ref": "baseline"}, {"ref": "candidate"}],
            "quality": {"minimum_repeats": 3, "minimum": 1, "maximum_peak_memory_mib": 200},
        },
        "observations": observations,
        "recommendation": {
            "created_at": "2026-01-01T00:00:20+00:00",
            "predicted_candidate": "candidate",
            "confirmed_candidate": "baseline",
            "confirmation_run_ids": [o["attempt_id"] for o in observations],
        },
    }


def test_only_uncertainty_retention_changes_and_input_is_not_mutated(study):
    before = deepcopy(study)
    output = module.replay(study)
    assert output["strict"] == "baseline"
    assert output["without_uncertainty"] == "candidate"
    assert len(output["ranking"]) == 2
    assert study == before


@pytest.mark.parametrize("fault", ["quality", "memory", "failed", "missing_repeat", "nan"])
def test_no_relaxation_of_feasibility_or_repeat_requirements(study, fault):
    row = study["observations"][-1]
    if fault == "quality":
        row["measurements"]["quality_value"] = 0.9
    elif fault == "memory":
        row["measurements"]["peak_memory_mib"] = 201
    elif fault == "failed":
        row["outcome"] = "OOM"
        row["measurements"] = None
    elif fault == "nan":
        row["measurements"]["elapsed_seconds"] = float("nan")
    else:
        study["observations"].pop()
        study["recommendation"]["confirmation_run_ids"].pop()
    output = module.replay(study)
    assert output["strict"] is output["without_uncertainty"] is None
    assert output["rejected"] == ["candidate"]


@pytest.mark.parametrize("fault", ["future", "unknown_candidate", "duplicate", "saved_decision"])
def test_rejects_leaked_or_changed_decision_evidence(study, fault):
    row = study["observations"][-1]
    if fault == "future":
        row["recorded_at"] = "2026-01-01T00:00:21+00:00"
    elif fault == "unknown_candidate":
        row["candidate_ref"] = "unapproved"
    elif fault == "duplicate":
        study["observations"].append(deepcopy(row))
    else:
        study["recommendation"]["confirmed_candidate"] = "candidate"
    with pytest.raises(ValueError):
        module.replay(study)


def test_existing_abstention_stays_an_abstention(study):
    study["recommendation"] = None
    output = module.replay(study)
    assert output["strict"] is output["without_uncertainty"] is None
    assert output["reason"] == "EXISTING_ABSTENTION_RETAINED"


def test_unconfirmed_and_pilot_options_are_never_added(study):
    pilot = deepcopy(study["observations"][0])
    pilot.update(mode="pilot", candidate_ref="unmeasured-finalist", attempt_id="pilot")
    pilot["measurements"]["elapsed_seconds"] = 0.001
    study["observations"].append(pilot)
    output = module.replay(study)
    assert output["without_uncertainty"] == "candidate"
    assert {r["candidate_ref"] for r in output["ranking"]} == {"baseline", "candidate"}
