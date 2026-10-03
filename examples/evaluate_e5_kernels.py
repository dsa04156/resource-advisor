"""Audit the fixed E5 trial from public raw evidence; never submit a workload."""

import argparse
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from resource_advisor.contracts import ExecutionResult, signature
from resource_advisor.diagnostics import diagnose, validate_profile
from resource_advisor.kernel_diagnostics import summarize_trace
from resource_advisor.thermal import assess, validate_trace


def evaluate(capture, plan):
    if capture["status"] != "COMPLETED" or len(capture["qualification"]) != 2:
        raise ValueError("complete, separately qualified trial required")
    predecessor = plan["predecessor"]
    prior_jobs = predecessor["v1_jobs"] + predecessor["capture_probe_jobs"]
    prior_gpu_seconds = predecessor["gpu_reservation_seconds"]
    if (
        capture["prior_protocol"]["GPU_jobs"] != prior_jobs
        or capture["prior_protocol"]["gpu_reservation_seconds"] != prior_gpu_seconds
        or [r["arm"] for r in capture["qualification"]] != plan["qualification"]
    ):
        raise ValueError("qualification order or retained predecessor costs changed")
    rows = capture["runs"]
    if [{k: r[k] for k in ("unit_id", "block", "arm")} for r in rows] != plan["schedule"]:
        raise ValueError("schedule changed or a run was omitted")
    all_rows = capture["qualification"] + rows
    if len(all_rows) != plan["maximum_jobs"]:
        raise ValueError("wrong number of retained Jobs")
    seen, diagnoses, fractions = set(), {}, {}
    for row in all_rows:
        result = ExecutionResult.model_validate(row["envelope"]["result"])
        identity, context = row["identity"], row["context"]
        if (
            result.attempt_id in seen
            or result.evidence_kind != "hardware"
            or result.outcome != "COMPLETED"
            or signature(result) != row["envelope"]["digest"]
            or result.workload_signature != signature(identity)
            or result.context_signature != signature(row["context_binding"])
            or context["resources"]
            != {"host_cpu": 1, "host_memory_mib": 2048, "accelerator_count": 1}
            or context["parameters"] != {"input_strategy": row["arm"].split("-")[0]}
            or identity["input_shape"] != [32, 3, 128, 128]
            or identity["seed"] != 20261009
            or identity["code_digest"] != capture["source_digest"]
            or result.measurements.quality_value != 1
            or result.measurements.peak_memory_mib > 2048
        ):
            raise ValueError("result identity, allocation or output qualification mismatch")
        if "unit_id" in row and row["arm"].endswith("-plain"):
            binding = row["context_binding"]
            if (
                binding["context"] != context
                or binding["command"] != ["python", "-m", "resource_advisor.e5_benchmark"]
                or binding["pilot_command"] != binding["command"]
                or binding["backend"] != "kubernetes"
                or binding["thermal_policy"] != capture["thermal_policy"]
            ):
                raise ValueError("API execution binding mismatch")
        elif row["context_binding"] != context:
            raise ValueError("direct diagnostic binding mismatch")
        seen.add(result.attempt_id)
        fixture = row["fixture"]
        for key, value in capture["fixture"].items():
            if fixture[key] != value:
                raise ValueError("model/input fixture changed")
        if fixture["model_weights_digest"] != identity["model_digest"]:
            raise ValueError("model digest differs from qualified identity")
        policy = capture["thermal_policy"]
        body = {
            "variant": {"thermal_policy": policy},
            "candidate": {"context": context},
            "spec": {"identity": identity},
        }
        trace = validate_trace(row["envelope"]["thermal_trace"], result, body)
        if assess(trace, policy)["status"] != "ELIGIBLE_TRACE":
            raise ValueError("ineligible thermal trace")
        phase = validate_profile(
            row["envelope"]["phase_profile"], result, identity["measurement_boundary"]
        )
        if len(phase.samples) != 12:
            raise ValueError("missing phase blocks")
        diagnoses[result.attempt_id] = diagnose(phase, evidence_kind="hardware")
        profiler = row["arm"].endswith("-profiler")
        if fixture["profiler_enabled"] != profiler:
            raise ValueError("profiler mixed into plain performance")
        if profiler:
            if row["ordinary_profile_created"]:
                raise ValueError("profiler timing entered ordinary profiles")
            parsed = summarize_trace(row["trace_input"], 12)
            recorded = row["envelope"]["kernel_diagnostics"]
            if parsed["kernel_count"] != recorded["kernel_count"]:
                raise ValueError("recorded kernel count differs from trace")
            for key in ("kernel_span_fraction", "kernel_union_seconds", "forward_span_seconds"):
                # Capture removes the absolute timestamp origin. Allow only the
                # sub-nanosecond floating subtraction rounding, not data drift.
                if not math.isclose(parsed[key], recorded[key], rel_tol=1e-7, abs_tol=1e-9):
                    raise ValueError("recorded kernel timing differs from trace")
            fractions[result.attempt_id] = parsed["kernel_span_fraction"]
        elif "unit_id" in row:
            checks = row["verification"]
            if not (
                checks["queue_admitted"]
                and checks["ledger_count"] == 1
                and checks["mlflow_count"] == 1
                and checks["matching_s3_api_mlflow_bytes"]
            ):
                raise ValueError("missing plain-run delivery/accounting evidence")
        cost = row["cost"]["gpu_reservation_seconds"]
        if type(cost) not in (int, float) or not 0 < cost <= plan["per_job_max_run_seconds"]:
            raise ValueError("unknown or out-of-budget allocation")
    gpu_seconds = math.fsum(r["cost"]["gpu_reservation_seconds"] for r in all_rows)
    wall_seconds = (
        datetime.fromisoformat(capture["completed_at"])
        - datetime.fromisoformat(capture["started_at"])
    ).total_seconds()
    if gpu_seconds > plan["maximum_gpu_reservation_seconds"] or not (
        0 < wall_seconds <= plan["maximum_wall_seconds"]
    ):
        raise ValueError("fixed experiment budget exceeded")
    groups = {}
    for arm in plan["arms"]:
        selected = [r for r in rows if r["arm"] == arm]
        elapsed = [r["envelope"]["result"]["measurements"]["elapsed_seconds"] for r in selected]
        if len(elapsed) != 3:
            raise ValueError("three independent Jobs per arm required")
        ids = [r["envelope"]["result"]["attempt_id"] for r in selected]
        groups[arm] = {
            "jobs": 3,
            "elapsed_seconds": elapsed,
            "mean_seconds": statistics.mean(elapsed),
            "sd_seconds": statistics.stdev(elapsed),
            "hypotheses": [diagnoses[i]["hypothesis"] for i in ids],
            "kernel_span_fractions": [fractions[i] for i in ids if i in fractions],
        }
    pairs = []
    for block in range(3):
        by_arm = {
            r["arm"]: r["envelope"]["result"]["measurements"]["elapsed_seconds"]
            for r in rows
            if r["block"] == block
        }
        pairs.append(
            {
                "block": block,
                "plain_recompute_minus_cache_seconds": by_arm["recompute-plain"]
                - by_arm["cache-plain"],
                "profiler_minus_plain_seconds": {
                    arm: by_arm[arm + "-profiler"] - by_arm[arm + "-plain"]
                    for arm in ("cache", "recompute")
                },
            }
        )
    criteria = {
        "input_supply_hypothesis_all_three": all(
            h == "POSSIBLE_INPUT_SUPPLY_BOUND" for h in groups["recompute-plain"]["hypotheses"]
        ),
        "accelerator_path_hypothesis_all_three": all(
            h == "POSSIBLE_ACCELERATOR_PATH_BOUND" for h in groups["cache-plain"]["hypotheses"]
        ),
        "cache_kernel_span_ge_60_percent_all_three": all(
            f >= 0.6 for f in groups["cache-profiler"]["kernel_span_fractions"]
        ),
    }
    return {
        "scope": "three-block functional E5 acceptance; not a powered superiority study",
        "criteria": criteria,
        "accepted": all(criteria.values()),
        "groups": groups,
        "paired_differences": pairs,
        "GPU_count_changed": False,
        "v2_gpu_reservation_seconds": gpu_seconds,
        "prior_failed_qualification_and_probe_gpu_seconds": prior_gpu_seconds,
        "all_protocol_gpu_reservation_seconds": gpu_seconds + prior_gpu_seconds,
        "v2_wall_seconds": wall_seconds,
        "total_jobs_including_prior_protocol": len(all_rows) + prior_jobs,
        "limitations": [
            "CUDA kernel union is not SM occupancy or arithmetic-versus-memory saturation.",
            "Repeated generated input does not justify caching changing datasets.",
            "Profiler-on measurements are not ordinary performance profiles.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--plan", type=Path, default=Path("docs/evidence/e5-kernel-plan-v2.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(json.loads(args.capture.read_text()), json.loads(args.plan.read_text()))
    data = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(data)
    else:
        print(data, end="")


if __name__ == "__main__":
    main()
