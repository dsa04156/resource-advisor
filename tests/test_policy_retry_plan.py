import copy
import importlib.util
import json
from pathlib import Path

import pytest

root = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "policy_retry", root / "examples/plan_policy_retry.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def inputs():
    return [
        json.loads((root / "docs/evidence" / name).read_text())
        for name in ["policy-comparison-plan.json", "policy-stop.json", "optimizer-repair.json"]
    ]


def test_new_trial_preserves_design_and_keeps_the_full_failed_cost():
    original, stopped, repair = inputs()
    untouched = copy.deepcopy(original)
    new = module.make_plan(original, stopped, repair)
    assert original == untouched
    for field in original.keys() - {"schema_version", "created_at"}:
        assert new[field] == original[field]
    assert new["predecessor"]["retained_gpu_reservation_seconds"] == 140
    assert new["predecessor"]["retained_application_jobs"] == 48
    assert new["runtime_requirements"]["worker_image_digest"] == repair["worker_image_digest"]
    assert new["schema_version"] != original["schema_version"]


@pytest.mark.parametrize("fault", ["live", "resumed", "missing-extra", "fallback", "partial-cost"])
def test_no_new_trial_without_a_retained_terminal_predecessor_and_actual_repair(fault):
    original, stopped, repair = inputs()
    if fault == "live":
        stopped["status"] = "running"
    elif fault == "resumed":
        repair["stopped_experiment_resumed"] = True
    elif fault == "missing-extra":
        repair["required_extras"] = ["artifacts"]
    elif fault == "fallback":
        repair["actual_model_check"]["reason"] = "MODEL_FAILURE_RANDOM_FALLBACK"
    else:
        stopped["results_ledger_api_s3_mlflow_verified"] = 47
    with pytest.raises(ValueError):
        module.make_plan(original, stopped, repair)


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf")])
def test_unknown_or_invalid_predecessor_cost_is_not_discarded(value):
    original, stopped, repair = inputs()
    stopped["application_gpu_reservation_seconds"] = value
    with pytest.raises(ValueError, match="allocation costs"):
        module.make_plan(original, stopped, repair)
