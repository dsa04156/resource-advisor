"""Recompute the bounded, fixed-order GPU drift acceptance from retained traces.

This checks the published observation record, not the running cluster or a
population causal effect. Scheduler/remote-storage checks remain observer evidence.
"""

import argparse
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from resource_advisor.contracts import ExecutionResult, signature
from resource_advisor.load_context import PROVIDER, summarize, validate_trace
from resource_advisor.thermal import assess
from resource_advisor.thermal import validate_trace as validate_thermal


def audit(report):
    assert report["schema_version"] == "load-drift-v1"
    runs = report["runs"]
    assert len(runs) == 9 and [r["index"] for r in runs] == list(range(9))
    assert [r["condition"] for r in runs] == ["normal"] * 3 + ["cpu-competitor"] * 3 + [
        "normal"
    ] * 3
    assert len({r["result"]["attempt_id"] for r in runs}) == 9
    assert len({r["result"]["job_id"] for r in runs}) == 9
    assert len({r["result"]["workload_signature"] for r in runs}) == 1
    assert len({r["result"]["context_signature"] for r in runs}) == 1
    qualification = report["qualification"]
    assert qualification["envelope"]["result"]["attempt_id"] not in {
        r["result"]["attempt_id"] for r in runs
    }
    durations, reserved = [], []
    all_records = [
        {
            "result": qualification["envelope"]["result"],
            "load_trace": qualification["envelope"]["load_trace"],
            "thermal_trace": qualification["envelope"]["thermal_trace"],
            "result_digest": qualification["envelope"]["digest"],
            "fixture": qualification["fixture"],
            "gpu_reservation_seconds": qualification["cost"]["gpu_reservation_seconds"],
        },
        *runs,
    ]
    for record in all_records:
        result = ExecutionResult.model_validate(record["result"])
        assert result.evidence_kind == "hardware" and result.outcome == "COMPLETED"
        assert signature(result) == record["result_digest"]
        m = result.measurements
        assert m.quality_value == 1 and m.work_units == m.sample_count == 12
        assert m.peak_memory_mib <= 2048
        assert record["fixture"] == qualification["fixture"]
        assert record["fixture"]["seed"] == 20261007
        assert record["fixture"]["shape"] == [1, 3, 256, 256]
        body = {
            "candidate": {
                "context": {
                    "resources": {"host_cpu": 0.5, "host_memory_mib": 2048},
                    "runtime_versions": {"driver": report["thermal_policy"]["driver_version"]},
                }
            },
            "variant": {
                "load_context_policy": PROVIDER,
                "thermal_policy": report["thermal_policy"],
            },
            "spec": {"identity": {"work_units": 12}},
        }
        load = validate_trace(record["load_trace"], result, body)
        assert load.before.cpu_quota_usec == 50000 and load.before.cpu_period_usec == 100000
        thermal = validate_thermal(record["thermal_trace"], result, body)
        assert assess(thermal, report["thermal_policy"])["status"] == "ELIGIBLE_TRACE"
        cost = record["gpu_reservation_seconds"]
        assert isinstance(cost, (int, float)) and math.isfinite(cost) and cost > m.elapsed_seconds
        assert cost <= 90
        reserved.append(cost)
    for record in runs:
        durations.append(record["result"]["measurements"]["elapsed_seconds"])
        assert record["load_context"] == summarize(record["load_trace"])
        assert record["matching_s3_api_mlflow_bytes"] is True
        assert record["unique_ledger_and_mlflow_run"] is True
        if record["condition"] == "cpu-competitor":
            starts = [x for x in record["helper_records"] if x["event"] == "start"]
            assert len(starts) == 1
            assert (
                starts[0]["cgroup_identity_digest"]
                == record["load_trace"]["cgroup_identity_digest"]
            )
            assert record["owned_compute_container_terminated"] is True
    baseline = statistics.mean(durations[:3])
    assert math.isclose(baseline, report["recommendation"]["mean_seconds"], rel_tol=1e-12)
    created = datetime.fromisoformat(report["recommendation"]["created_at"])
    assert all(datetime.fromisoformat(r["verified_at"]) <= created for r in runs[:3])
    assert all(datetime.fromisoformat(r["started_at"]) > created for r in runs[3:])
    residuals = [(v - baseline) / baseline for v in durations[3:]]
    latched, first_trigger = False, None
    for i, record in enumerate(runs[3:]):
        if i >= 2:
            block = residuals[i - 2 : i + 1]
            if all(x > 0.25 for x in block) or all(x < -0.25 for x in block):
                latched = True
                first_trigger = i + 3 if first_trigger is None else first_trigger
        validity = record["recommendation_validity"]
        assert ("CONSECUTIVE_RESIDUAL_DRIFT" in validity["reasons"]) == latched
        if latched:
            assert validity["reusable"] is False
    assert report["drift_triggered"] == latched
    if latched:
        assert report["negative_approval"]["status"] == 422
        assert report["negative_approval_created_jobs"] == 0
        if first_trigger == 5:
            assert report["transition_lookup"]["measured"] is False
    assert sum(reserved) <= 990
    assert math.isclose(sum(reserved), report["gpu_seconds"], rel_tol=1e-12)
    wall = (
        datetime.fromisoformat(report["completed_at"])
        - datetime.fromisoformat(report["started_at"])
    ).total_seconds()
    assert 0 < wall <= 1800
    return {
        "independent_gpu_jobs": 10,
        "baseline_mean_seconds": baseline,
        "competitor_mean_seconds": statistics.mean(durations[3:6]),
        "recovery_mean_seconds": statistics.mean(durations[6:]),
        "followup_relative_residuals": residuals,
        "first_drift_slot_zero_based": first_trigger,
        "drift_remained_latched": latched,
        "gpu_reservation_seconds": sum(reserved),
        "protocol_wall_seconds": wall,
        "scope": "fixed-order functional acceptance; no randomized causal or calibrated probability claim",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(json.loads(args.evidence.read_text())), indent=2))
