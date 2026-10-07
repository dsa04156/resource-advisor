"""Cost provenance and protocol bounds, using retained raw captures."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from audit_right_sizing_cost import audit_cost  # noqa: E402


@pytest.fixture
def capture():
    root = Path(__file__).resolve().parents[1] / "docs/evidence"
    return json.loads((root / "right-sizing-total-cost-capture-v1.json").read_text()), root


def test_recomputed_total_retains_failure_and_separate_device_units(capture):
    value, root = capture
    checked = audit_cost(value, root)
    assert checked["native_total_jobs"] == checked["native_gpu_jobs"] + checked["native_npu_jobs"]
    assert checked["failed_gpu_jobs"] > 0
    assert checked["gpu_reservation_seconds"] > 0 and checked["npu_reservation_seconds"] > 0
    assert checked["unknown"] and checked["overall_goal_complete"] is False


@pytest.mark.parametrize("fault", ["digest", "experiment", "budget", "end"])
def test_cost_auditor_rejects_inconsistent_clock_and_scope(capture, fault):
    original, root = capture
    value = copy.deepcopy(original)
    if fault == "digest":
        value["input_sha256"]["right-sizing-gpu-v1.json"] = "0" * 64
    elif fault == "experiment":
        value["primary_experiment_id"] = "other"
    elif fault == "budget":
        value["protocols"]["primary"]["started_at"] = "2000-01-01T00:00:00+00:00"
    else:
        value["protocols"]["primary"]["ended_at"] = "2099-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        audit_cost(value, root)
