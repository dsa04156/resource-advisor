"""Mutation checks on captured hardware evidence; these run no hardware."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def auditor():
    import sys

    sys.path.insert(0, str(ROOT / "examples"))
    try:
        spec = importlib.util.spec_from_file_location(
            "right_sizing_auditor", ROOT / "examples/audit_right_sizing.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def test_preserved_baseline_audit_is_not_new_hardware_completion():
    report = auditor().audit(ROOT)
    assert report["new_hardware_jobs"] == 0
    assert not report["overall_goal_complete"]
    assert len(report["inputs"]) == 10


@pytest.mark.parametrize("fault", ["digest", "missing_cost", "reused_attempt", "budget", "future"])
def test_composite_auditor_rejects_corrupt_policy_raw_evidence(tmp_path, fault):
    module = auditor()
    output = tmp_path / "docs/evidence"
    output.mkdir(parents=True)
    for name in module.INPUTS.values():
        (output / name).symlink_to(ROOT / "docs/evidence" / name)
    path = output / module.INPUTS["policy"]
    data = json.loads(path.read_text())
    row = data["observations"][0]
    if fault == "digest":
        row["result"]["context_signature"] = "sha256:" + "0" * 64
    elif fault == "missing_cost":
        row["allocated_device_seconds"] = None
    elif fault == "reused_attempt":
        data["observations"][1]["attempt_id"] = row["attempt_id"]
    elif fault == "budget":
        data["plan"]["budgets"]["max_probes"] = 0
    else:
        row["submitted_at"] = "2099-01-01T00:00:00+00:00"
    path.unlink()
    path.write_text(json.dumps(data))
    with pytest.raises((ValueError, KeyError, TypeError)):
        module.audit(tmp_path)
