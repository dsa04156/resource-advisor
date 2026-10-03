import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "plan_operational_comparison", ROOT / "examples/plan_operational_comparison.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_frozen_operational_schedule_is_balanced_reproducible_and_bounded():
    plan = module.make_plan()
    assert plan == json.loads((ROOT / "docs/evidence/operational-plan-v1.json").read_text())
    for block in range(plan["blocks"]):
        assert Counter(s["arm"] for s in plan["schedule"] if s["block"] == block) == {
            "B0": 1,
            "B1": 1,
            "B2": 1,
        }
    assert plan["budgets"]["total_jobs_maximum"] == 35
    assert plan["workload"]["baseline_cpu"] in plan["workload"]["candidate_cpu_cores"]
    assert max(plan["workload"]["candidate_cpu_cores"]) <= plan["fixed_environment"]["quota_cpu"]
    assert plan["workload"]["gpu_count"] == plan["fixed_environment"]["quota_physical_gpu"] == 1


@pytest.mark.parametrize("blocks", [0, 2, 7, True])
def test_unapproved_trial_size_rejected(blocks):
    with pytest.raises(ValueError):
        module.make_plan(blocks=blocks)
