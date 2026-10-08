"""Replay captured evidence and deliberately corrupt copies; never submit hardware jobs."""

import copy
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
audit = runpy.run_path(str(ROOT / "examples/audit_accelerator_enablement.py"))["audit"]


@pytest.fixture
def records():
    evidence = ROOT / "docs/evidence"
    return (
        json.loads((evidence / "all-accelerators-20261008-v1.json").read_text()),
        json.loads((evidence / "all-accelerators-20261008-native-proof-v2.json").read_text()),
    )


def test_unknown_cost_rejects_complete_cost_claim(records):
    with pytest.raises(ValueError, match="complete-cost claim rejected"):
        audit(*records)
    report = audit(*records, allow_incomplete_cost=True)
    assert report["api_cost_complete"] is False
    assert report["unknown_cost_attempts"]
    assert report["performance_improvement_claim"] is False
    assert report["overall_right_sizing_goal_complete"] is False
    assert sum(report["states"].values()) == len(records[0]["jobs"])


@pytest.mark.parametrize(
    "corruption",
    ["duplicate", "digest", "quality", "reservation", "native_id", "native_outcome", "native_time"],
)
def test_corrupted_captures_are_rejected(records, corruption):
    report, receipts = copy.deepcopy(records)
    row = next(r for r in report["jobs"] if r["state"] == "SUCCEEDED")
    if corruption == "duplicate":
        report["jobs"].append(copy.deepcopy(row))
    elif corruption == "digest":
        row["result_digest"] = "sha256:" + "0" * 64
    elif corruption == "quality":
        row["quality_passed"] = False
    elif corruption == "reservation":
        row["usage"]["allocated_device_seconds"] += 1
    elif corruption == "native_id":
        receipts[row["attempt_id"]]["job_id"] = "unrelated-job"
    elif corruption == "native_outcome":
        receipts[row["attempt_id"]]["phase"] = "Failed"
        receipts[row["attempt_id"]]["container"]["exitCode"] = 42
    else:
        from datetime import datetime, timedelta

        u = row["usage"]
        for key in (
            "allocation_interval_seconds",
            "allocated_device_seconds",
            "allocated_cpu_seconds",
        ):
            u[key] += 1
        u["backend_finished_at"] = (
            datetime.fromisoformat(u["backend_finished_at"].replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat()
    with pytest.raises(ValueError):
        audit(report, receipts, allow_incomplete_cost=True)
