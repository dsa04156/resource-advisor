"""Leakage/abstention/variance contracts; synthetic inputs are not GPU evidence."""

import importlib.util
import json
import math
import statistics
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from resource_advisor import workload_uncertainty as model

ROOT = Path(__file__).parents[1]
for name in ("evaluate_transfer", "evaluate_workload_holdout", "evaluate_numerical_holdout"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


@pytest.fixture
def inputs():
    cells = []
    for area in (16, 64):
        for cpu in (0.5, 1, 2):
            descriptor = model.Target(f"workload-{area}", "same-family", f"cpu{cpu}", area, cpu)
            cells.append(
                model.SourceCell(
                    descriptor,
                    tuple(f"{area}-{cpu}-{i}" for i in range(3)),
                    tuple((area / 16 + 1 / cpu) * t for t in (0.9, 1, 1.1)),
                )
            )
    targets = [model.Target("held-out", "same-family", f"cpu{cpu}", 36, cpu) for cpu in (0.5, 1, 2)]
    return cells, targets


def test_scaling_and_training_noise_use_only_sources(inputs):
    cells, targets = inputs
    bounds, prepared, _ = model.prepare(cells, targets)
    second = model.prepare(cells, [replace(t, input_area=1_000_000) for t in targets])
    assert (bounds, prepared) == second[:2]
    assert bounds == {"input_area": [16, 64], "host_cpu": [0.5, 2]}
    expected = statistics.variance(math.log(t) for t in cells[0].elapsed_seconds)
    assert prepared[0]["training_variance"] == pytest.approx(expected / 3)
    assert prepared[0]["log_sample_variance"] == pytest.approx(expected)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"input_area": 8}, "OUTSIDE_SOURCE_RANGE"),
        ({"input_area": 100}, "OUTSIDE_SOURCE_RANGE"),
        ({"family_signature": "new-runtime"}, "UNSEEN_FAMILY_OR_RUNTIME"),
        ({"candidate_ref": "cpu0.75", "host_cpu": 0.75}, "UNSEEN_CONFIGURATION"),
    ],
)
def test_unsupported_targets_abstain_without_importing_or_fitting(
    inputs, monkeypatch, change, reason
):
    def forbidden(*args):
        pytest.fail("unsupported target reached model fitting")

    monkeypatch.setattr(model, "_fit_predict", forbidden)
    result = model.forecast(inputs[0], [replace(inputs[1][0], **change)])
    assert result["fit_count"] == 0
    assert all(r["prediction"] is None and reason in r["reasons"] for r in result["targets"])


def test_target_workload_overlap_is_rejected_even_with_disjoint_attempts(inputs):
    cells, targets = inputs
    cells[0] = replace(
        cells[0], descriptor=replace(cells[0].descriptor, workload_signature="held-out")
    )
    with pytest.raises(ValueError, match="leaked"):
        model.forecast(cells, targets)


def test_duplicate_attempt_and_incomplete_grid_rejected(inputs):
    cells, targets = inputs
    with pytest.raises(ValueError, match="incomplete"):
        model.prepare(cells[:-1], targets)
    cells[1] = replace(cells[1], attempt_ids=cells[0].attempt_ids)
    with pytest.raises(ValueError, match="duplicate.*attempt"):
        model.prepare(cells, targets)


@pytest.mark.parametrize("value", [False, 0, -1, float("inf"), float("nan")])
def test_invalid_measurements_are_not_fast_training_results(inputs, value):
    cells, targets = inputs
    cells[0] = replace(cells[0], elapsed_seconds=(value, 1, 2))
    with pytest.raises(ValueError, match="finite positive"):
        model.prepare(cells, targets)


def test_unknown_source_runtime_and_alias_workloads_rejected(inputs):
    cells, targets = inputs
    changed = [
        replace(c, descriptor=replace(c.descriptor, family_signature="other")) if i == 0 else c
        for i, c in enumerate(cells)
    ]
    with pytest.raises(ValueError, match="mixed source"):
        model.prepare(changed, targets)
    changed = [replace(c, descriptor=replace(c.descriptor, input_area=16)) for c in cells]
    with pytest.raises(ValueError, match="aliases"):
        model.prepare(changed, targets)


def test_alias_or_inconsistent_target_shape_is_not_workload_holdout(inputs):
    cells, targets = inputs
    with pytest.raises(ValueError, match="already present"):
        model.prepare(cells, [replace(t, input_area=16) for t in targets])
    targets[0] = replace(targets[0], input_area=40)
    with pytest.raises(ValueError, match="target workload descriptor changed"):
        model.prepare(cells, targets)


def test_model_failure_does_not_leave_partial_predictions_or_substitute_model(inputs, monkeypatch):
    def fail(cells, bounds, eligible, result):
        eligible[0].update(status="SHADOW_PREDICTION", prediction={"partial": True})
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(model, "_fit_predict", fail)
    result = model.forecast(*inputs)
    assert result["model_failure"] == "RuntimeError"
    assert all(
        r["prediction"] is None and r["reasons"] == ["MODEL_FAILURE"] for r in result["targets"]
    )
    assert result["execution_authorized"] is False


def test_real_gp_individual_variance_is_not_divided_by_repeat_count(inputs):
    torch = pytest.importorskip("torch")
    pytest.importorskip("botorch")
    state, threads = torch.get_rng_state(), torch.get_num_threads()
    original = deepcopy(inputs)
    result = model.forecast(*inputs)
    assert result["fit_count"] == 1
    assert result["status_counts"] == {"SHADOW_PREDICTION": 3}
    assert torch.equal(state, torch.get_rng_state()) and torch.get_num_threads() == threads
    assert inputs == original
    for target in result["targets"]:
        p = target["prediction"]
        expected_noise = statistics.mean(
            statistics.variance(math.log(t) for t in cell.elapsed_seconds)
            for cell in inputs[0]
            if cell.descriptor.host_cpu == target["descriptor"]["host_cpu"]
        )
        assert p["individual_noise_log_variance"] == pytest.approx(expected_noise)
        latent, job = p["latent_interval_seconds"], p["job_interval_seconds"]
        assert job[0] < latent[0] <= p["median_seconds"] <= latent[1] < job[1]


def test_warning_and_random_state_restored_on_gp_failure(inputs, monkeypatch):
    torch = pytest.importorskip("torch")
    botorch = pytest.importorskip("botorch.fit")
    import warnings

    def fail(*args, **kwargs):
        warnings.warn("synthetic failed fit", RuntimeWarning, stacklevel=2)
        raise RuntimeError("fit error")

    monkeypatch.setattr(botorch, "fit_gpytorch_mll", fail)
    state, threads = torch.get_rng_state(), torch.get_num_threads()
    result = model.forecast(*inputs)
    assert torch.equal(state, torch.get_rng_state()) and torch.get_num_threads() == threads
    assert any(w["message"] == "synthetic failed fit" for w in result["diagnostics"])
    assert result["status_counts"] == {"ABSTAIN": 3}


def test_evaluator_changes_heldout_values_only_after_prediction_boundary(monkeypatch):
    capture = json.loads((ROOT / "docs/evidence/transfer-gpu.json").read_text())
    audited, cohorts = module.prepare_capture(capture)
    target = cohorts["source-1"]
    source = [c for key, cells in cohorts.items() if key != "source-1" for c in cells]
    calls = []

    def stub(cells, bounds, eligible, result):
        calls.append((deepcopy(cells), deepcopy(bounds), [r["descriptor"] for r in eligible]))
        for row in eligible:
            row.update(
                status="SHADOW_PREDICTION",
                prediction={
                    "median_seconds": 1,
                    "latent_interval_seconds": [0.9, 1.1],
                    "job_interval_seconds": [0.8, 1.2],
                },
            )

    monkeypatch.setattr(model, "_fit_predict", stub)
    before = model.forecast(source, [c.descriptor for c in target])
    changed = [replace(c, elapsed_seconds=tuple(t * 10 for t in c.elapsed_seconds)) for c in target]
    after = model.forecast(source, [c.descriptor for c in changed])
    assert calls[0] == calls[1] and before["targets"] == after["targets"]
    rows = {r["attempt_id"]: r for r in capture["observations"]}
    quality = capture["studies"]["oracle"]["spec"]["quality"]
    original_score = module.score_targets(before, target, rows, quality)
    changed_score = module.score_targets(after, changed, rows, quality)
    assert original_score["summary"] != changed_score["summary"]
    # A failed constraint is counted even on an abstained target.
    abstained = model.forecast(
        [c for k, v in cohorts.items() if k != "oracle" for c in v],
        [c.descriptor for c in cohorts["oracle"]],
    )
    rows[cohorts["oracle"][0].attempt_ids[0]]["measurements"]["quality_value"] = 0
    score = module.score_targets(abstained, cohorts["oracle"], rows, quality)["summary"]
    assert score["constraint_violations_all_completed_targets"] == 1
    assert score["job"]["coverage_fraction"] is None
    assert score["predicted_jobs"] == 0 and score["abstained_jobs"] == 9


def test_published_numerical_report_reproduces_without_mutating_original_capture():
    pytest.importorskip("botorch")
    capture = json.loads((ROOT / "docs/evidence/transfer-gpu.json").read_text())
    original = deepcopy(capture)
    actual = module.evaluate(capture)
    published = json.loads((ROOT / "docs/evidence/numerical-workload-holdout-v1.json").read_text())

    def compare(a, b):
        if isinstance(a, dict):
            assert a.keys() == b.keys()
            for key in a:
                if key == "modeling_wall_seconds":
                    assert a[key] >= 0 and b[key] >= 0
                else:
                    compare(a[key], b[key])
        elif isinstance(a, list):
            assert len(a) == len(b)
            for x, y in zip(a, b, strict=True):
                compare(x, y)
        elif isinstance(a, float):
            assert a == pytest.approx(b, rel=1e-6, abs=1e-9)
        else:
            assert a == b

    compare(actual, published)
    assert capture == original
    assert (
        actual["chronological_saved_forecasts"]
        == json.loads((ROOT / "docs/evidence/workload-holdout-v1.json").read_text())[
            "chronological_forecasts"
        ]
    )
    for fold in actual["whole_workload_folds"]:
        source_ids = {a for c in fold["forecast"]["source_cells"] for a in c["attempt_ids"]}
        target_ids = {r["attempt_id"] for r in fold["scored"] + fold["excluded"]}
        assert len(source_ids) == 18 and len(target_ids) == 9 and source_ids.isdisjoint(target_ids)
    assert actual["prediction_status_counts"] == {"ABSTAIN": 6, "SHADOW_PREDICTION": 3}
