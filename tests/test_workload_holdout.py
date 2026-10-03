"""Reject leakage and misattribution; synthetic fold tests are not hardware results."""

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from resource_advisor.contracts import signature

ROOT = Path(__file__).parents[1]
for name in ("evaluate_transfer", "evaluate_workload_holdout"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


@pytest.fixture
def capture():
    return json.loads((ROOT / "docs/evidence/transfer-gpu.json").read_text())


@pytest.fixture
def folds():
    cohorts = {}
    for i in range(3):
        rows = []
        for ref, value in (("small", 3), ("medium", 2), ("large", 1)):
            for j in range(3):
                rows.append(
                    {
                        "attempt_id": f"task-{i}-{ref}-{j}",
                        "candidate_ref": ref,
                        "outcome": "COMPLETED",
                        "submitted_at": f"2026-01-01T00:0{i}:00+00:00",
                        "finished_at": f"2026-01-01T00:0{i}:10+00:00",
                        "recorded_at": f"2026-01-01T00:0{i}:11+00:00",
                        "measurements": {
                            "elapsed_seconds": value,
                            "peak_memory_mib": 100,
                            "quality_value": 1,
                        },
                    }
                )
        cohorts[str(i)] = {"signature": signature(i), "shape": [i + 1], "rows": rows}
    return cohorts


def run_fold(folds, held_out="2"):
    return module.rank_fold(
        folds,
        held_out,
        runtime_group=signature("synthetic-runtime"),
        options=[
            {"candidate_ref": ref, "coordinates": [value]}
            for ref, value in (("small", 0), ("medium", 0.5), ("large", 1))
        ],
        quality={"minimum": 1, "maximum_peak_memory_mib": 200},
    )


def test_held_out_values_cannot_affect_source_order_or_normalization(folds):
    before = run_fold(folds)
    for row in folds["2"]["rows"]:
        row["measurements"]["elapsed_seconds"] = 100 if row["candidate_ref"] == "large" else 1
    after = run_fold(folds)
    assert before["prior"] == after["prior"]
    assert before["finite_sample_regret_fraction"] == 0
    assert after["finite_sample_regret_fraction"] == 99
    assert after["prior"]["target_run_ids"] == []
    assert set(after["held_out_attempt_ids"]).isdisjoint(after["prior"]["source_run_ids"])
    assert after["interval_coverage"] is after["time_prediction"] is None


def test_future_source_fold_is_labeled_retrospective(folds):
    assert run_fold(folds)["source_precedes_target"] is True
    assert run_fold(folds, "0")["source_precedes_target"] is False


def test_source_quality_failure_is_not_silently_filtered(folds):
    folds["0"]["rows"][0]["measurements"]["quality_value"] = 0
    with pytest.raises(ValueError, match="infeasible source"):
        run_fold(folds)


def test_held_out_quality_failure_is_reported_without_changing_prior(folds):
    prior = run_fold(folds)["prior"]
    folds["2"]["rows"][-1]["measurements"]["quality_value"] = 0
    result = run_fold(folds)
    assert result["prior"] == prior
    assert result["selected_constraint_violations"] == 1
    assert result["finite_sample_regret_fraction"] is None


def test_repeated_attempt_cannot_cross_workload_boundary(folds):
    folds["2"]["rows"][0]["attempt_id"] = folds["0"]["rows"][0]["attempt_id"]
    with pytest.raises(ValueError, match="overlap"):
        run_fold(folds)


def test_distinct_labels_do_not_make_distinct_tasks(folds):
    folds["2"]["signature"] = folds["0"]["signature"]
    with pytest.raises(ValueError, match="distinct"):
        run_fold(folds)


def test_actual_capture_audit_is_read_only_and_covers_every_attempt(capture):
    original = deepcopy(capture)
    result = module.evaluate(capture)
    temporal = result["chronological_forecasts"]
    assert len(temporal["accepted"]) + len(temporal["excluded"]) == len(capture["observations"])
    assert all(not r["whole_workload_holdout"] for r in temporal["accepted"])
    assert len(result["whole_workload_rank_holdout"]) == 3
    assert capture == original
    published = json.loads((ROOT / "docs/evidence/workload-holdout-v1.json").read_text())
    assert result == published


@pytest.mark.parametrize("field", ["result_digest", "workload_signature", "measurements"])
def test_modified_captured_result_rejected(capture, field):
    row = capture["observations"][0]
    if field == "result_digest":
        row[field] = signature("tampered")
    elif field == "workload_signature":
        row["result"][field] = signature("different-workload")
        row["result_digest"] = signature(row["result"])
    else:
        row["result"][field]["elapsed_seconds"] *= 2
    with pytest.raises(ValueError, match="digest or identity"):
        module.evaluate(capture)


@pytest.mark.parametrize("field", ["submitted_at", "started_at", "recorded_at"])
def test_execution_time_leakage_rejected(capture, field):
    row = capture["observations"][0]
    row[field] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        module.evaluate(capture)


def test_source_profile_result_binding_is_checked(capture):
    capture["source_evidence"]["provenance"][0]["result_digest"] = signature("tampered")
    with pytest.raises(ValueError, match="source profile/result"):
        module.evaluate(capture)


def test_forecast_cannot_use_its_target_as_training(capture):
    plan = next(p for p in capture["plans"] if p["choice"].get("predictions"))
    target = next(r for r in capture["observations"] if r["plan_ref"] == plan["ref"])
    plan["choice"]["surrogate"]["training_run_ids"].append(target["attempt_id"])
    with pytest.raises(ValueError):
        module.evaluate(capture)


def test_duplicate_training_ids_rejected(capture):
    plan = next(p for p in capture["plans"] if p["choice"].get("predictions"))
    ids = plan["choice"]["surrogate"]["training_run_ids"]
    ids.append(ids[0])
    with pytest.raises(ValueError, match="duplicate training"):
        module.evaluate(capture)


@pytest.mark.parametrize("interval", [[False, 1], [None, 1], [2, 1], None])
def test_invalid_interval_is_explicitly_excluded(capture, interval):
    plan = next(p for p in capture["plans"] if p["choice"].get("predictions"))
    pred = next(
        p for p in plan["choice"]["predictions"] if p["candidate_ref"] == plan["candidate_ref"]
    )
    pred["posterior_interval_seconds"] = interval
    result = module.evaluate(capture)
    assert result["chronological_forecasts"]["exclusion_counts"]["INVALID_FORECAST"] == 1


def test_nonfinite_capture_cannot_receive_a_canonical_digest(capture):
    plan = next(p for p in capture["plans"] if p["choice"].get("predictions"))
    plan["choice"]["predictions"][0]["predicted_elapsed_seconds"] = float("inf")
    with pytest.raises(ValueError, match="JSON compliant"):
        module.evaluate(capture)


def test_duplicate_candidate_forecast_rejected(capture):
    plan = next(p for p in capture["plans"] if p["choice"].get("predictions"))
    pred = next(
        p for p in plan["choice"]["predictions"] if p["candidate_ref"] == plan["candidate_ref"]
    )
    plan["choice"]["predictions"].append(deepcopy(pred))
    with pytest.raises(ValueError, match="duplicate candidate"):
        module.evaluate(capture)
