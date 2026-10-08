"""Audit captured native API execution evidence; no performance comparison or live submission."""

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from resource_advisor.contracts import ExecutionResult, signature
from resource_advisor.uncertainty import stamp


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("missing/nonfinite API reservation cost")
    if value < 0:
        raise ValueError("negative API reservation cost")
    return value


def audit(report, receipts, *, allow_incomplete_cost=False):
    if report.get("scope") != "native-api-cohort-only" or report.get("synthetic") is not False:
        raise ValueError("not a captured hardware API cohort")
    attempts, jobs, native_uids = set(), set(), set()
    coverage, states, reservations = defaultdict(set), Counter(), defaultdict(float)
    failures, unknown_cost = [], []
    for row in report["jobs"]:
        job, attempt = row["job_id"], row["attempt_id"]
        receipt = receipts[attempt]
        native = receipt["job_uid"]
        if (
            receipt["job_id"] != job
            or row["native_job_id"] != attempt
            or (row["native_job_uid"] and row["native_job_uid"] != native)
        ):
            raise ValueError("inconsistent native termination receipt")
        if receipt["source"] == "current-retained-native-job":
            if receipt["context_signature"] != row["context_signature"] or not any(
                c["type"] == "Complete" and c["status"] == "True" for c in receipt["conditions"]
            ):
                raise ValueError("native Job context/terminal proof mismatch")
        elif receipt["source"] != "captured-termination-receipt":
            raise ValueError("unknown native proof source")
        if (
            not job
            or not attempt
            or not native
            or job in jobs
            or attempt in attempts
            or native in native_uids
        ):
            raise ValueError("missing/duplicate native job or attempt")
        jobs.add(job)
        attempts.add(attempt)
        native_uids.add(native)
        state, usage = row["state"], row["usage"]
        if state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise ValueError("nonterminal cohort")
        if usage["attempt_id"] != attempt or usage["job_id"] != job or usage["outcome"] != state:
            raise ValueError("inconsistent usage ownership/outcome")
        requested, allocation = row["requested_resources"], usage["observed_allocation"]
        if requested != usage["requested_resources"]:
            raise ValueError("mismatched requested reservation")
        if (
            receipt["source"] == "captured-termination-receipt"
            and receipt["allocation"] != allocation
        ):
            raise ValueError("termination allocation differs from usage")
        missing = not allocation or any(
            usage[k] is None
            for k in (
                "allocation_interval_seconds",
                "allocated_device_seconds",
                "allocated_cpu_seconds",
            )
        )
        if missing:
            if not allow_incomplete_cost or state == "SUCCEEDED":
                raise ValueError("missing API reservation cost; complete-cost claim rejected")
            unknown_cost.append(attempt)
            interval = device = cpu = None
        else:
            interval = number(usage["allocation_interval_seconds"])
            device = number(usage["allocated_device_seconds"])
            cpu = number(usage["allocated_cpu_seconds"])
            if not math.isclose(
                device, interval * number(allocation["accelerator_count"])
            ) or not math.isclose(cpu, interval * number(allocation["cpu"])):
                raise ValueError("reservation arithmetic mismatch")
            if usage["allocation_memory_mib"] != allocation["memory_mib"]:
                raise ValueError("memory reservation mismatch")
        submitted, finished = stamp(row["created_at"]), stamp(row["finished_at"])
        if not submitted or not finished or finished < submitted:
            raise ValueError("missing/reversed timestamps")
        if usage["started_at"] and usage["backend_finished_at"]:
            start, end = stamp(usage["started_at"]), stamp(usage["backend_finished_at"])
            if (
                not start
                or not end
                or end < start
                or not math.isclose((end - start).total_seconds(), interval)
            ):
                raise ValueError("reservation timing mismatch")
        states[state] += 1
        if not missing:
            reservations[row["device_class"] + "_seconds"] += device
            reservations["cpu_core_seconds"] += cpu
        if state == "SUCCEEDED":
            result = ExecutionResult.model_validate(row["result"])
            if (
                signature(row["result"]) != row["result_digest"]
                or result.job_id != job
                or result.attempt_id != attempt
                or result.workload_signature != row["workload_signature"]
                or result.context_signature != row["context_signature"]
            ):
                raise ValueError("result digest/identity mismatch")
            if (
                not result.measured
                or result.evidence_kind != "hardware"
                or result.outcome != "COMPLETED"
                or row["quality_passed"] is not True
                or not usage["result_valid"]
            ):
                raise ValueError("invalid hardware/quality result")
            m, q = result.measurements, row["quality_contract"]
            if (
                m is None
                or m.quality_value < q["minimum"]
                or m.peak_memory_mib > q["maximum_peak_memory_mib"]
                or m.elapsed_seconds <= 0
            ):
                raise ValueError("failed quality/memory/runtime constraint")
            if usage["measured_compute_seconds"] != m.elapsed_seconds:
                raise ValueError("measured runtime mismatch")
            if (
                not row["profile_recorded"]
                or not row["mlflow_run_id"]
                or not row["artifact_digest"]
                or row["artifact_tracking_digest"] != row["artifact_digest"]
            ):
                raise ValueError("missing profile/tracking/artifact linkage")
            coverage[row["device_class"]].add(row["node_alias"])
        else:
            failures.append(
                {
                    "job_id": job,
                    "reason": row["error"],
                    "device_seconds": device,
                    "cpu_core_seconds": cpu,
                    "compute_seconds": usage["measured_compute_seconds"],
                }
            )
    return {
        "states": dict(states),
        "successful_nodes": {k: sorted(v) for k, v in coverage.items()},
        "known_reservations": dict(reservations),
        "unknown_cost_attempts": unknown_cost,
        "api_cost_complete": not unknown_cost,
        "failures": failures,
        "cost_scope": "API cohort only; qualification/diagnostic/control effort is not a complete cost ledger",
        "performance_improvement_claim": False,
        "overall_right_sizing_goal_complete": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument(
        "--allow-incomplete-cost",
        action="store_true",
        help="Report partial known costs explicitly; never treat missing costs as zero",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(
        json.loads(args.evidence.read_text()),
        json.loads(args.receipts.read_text()),
        allow_incomplete_cost=args.allow_incomplete_cost,
    )
    if args.output:
        with args.output.open("x") as target:
            json.dump(result, target, indent=2)
            target.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
