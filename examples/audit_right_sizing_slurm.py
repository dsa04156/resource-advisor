"""Recompute fixed-candidate Slurm feedback from captured native evidence.

This is a lifecycle audit, not a search-strategy or cross-backend speedup claim.
No scheduler calls, submissions or synthetic measurements are performed.
"""

import argparse
import json
import math
import statistics
from pathlib import Path

from audit_right_sizing_trial import cost, require

from resource_advisor.contracts import (
    Candidate,
    CapabilitySnapshot,
    ExecutionResult,
    RuntimeVariant,
    WorkloadSpec,
    signature,
)
from resource_advisor.policy import compatibility, context_signature
from resource_advisor.uncertainty import stamp

FIELDS = (
    "job_id job_name account partition node state exit_code submit start end "
    "alloc_tres elapsed_seconds cpu_reservation_seconds max_rss total_cpu"
).split()


def native_rows(text):
    rows = []
    for line in text.splitlines():
        values = line.split("|")
        require(len(values) == len(FIELDS), "native accounting column mismatch")
        rows.append(dict(zip(FIELDS, values, strict=True)))
    return rows


def digest_body(value):
    return signature({k: v for k, v in value.items() if k != "digest"})


def audit(report, plan):
    require(report["plan_digest"] == signature(plan), "frozen plan mismatch")
    require(report["experiment_id"] == plan["experiment_id"], "experiment ID mismatch")
    expected_count = (
        plan["qualification"]["count"] + plan["observe"]["count"] + plan["main"]["count"]
    )
    require(
        len(report["attempts"]) == expected_count <= plan["budget"]["max_native_jobs"],
        "native attempt budget/count mismatch",
    )
    require(report["contracts"]["workload"] == plan["workload"], "candidate space changed")
    expected_variant = dict(plan["variant"])
    expected_variant["validation_refs"] = [report["attempts"][0]["attempt_id"]]
    require(report["contracts"]["variant"] == expected_variant, "runtime variant changed")
    cold = report["cold_start"]
    require(
        not cold["recommendation"]["measured"] and cold["lifecycle"]["state"] == "NEEDS_PROFILE",
        "unknown workload did not abstain",
    )
    attempts, native_ids, job_ids = {}, set(), set()
    gpu_seconds, cpu_seconds, memory_seconds = 0.0, 0.0, 0.0
    for index, row in enumerate(report["attempts"]):
        ref = row["attempt_id"]
        require(ref not in attempts and row["job_id"] not in job_ids, "duplicate attempt/job")
        attempts[ref] = row
        job_ids.add(row["job_id"])
        require(row["native_job_id"] not in native_ids, "duplicate native attempt")
        native_ids.add(row["native_job_id"])
        expected_role = (
            "qualification" if index == 0 else "observe" if index < 4 else "approved_main"
        )
        require(row["role"] == expected_role, "attempt role/count mismatch")
        spec = WorkloadSpec.model_validate(row["spec"])
        variant = RuntimeVariant.model_validate(row["variant"])
        candidate = Candidate.model_validate(row["candidate"])
        cap = CapabilitySnapshot.model_validate(row["capability"])
        require(candidate in spec.candidates, "candidate outside workload")
        require(candidate.backend == cap.backend == "slurm", "backend scope mismatch")
        require(
            variant.image is None
            and variant.command == tuple(plan["variant"]["command"])
            and variant.environment_digest == plan["variant"]["environment_digest"],
            "native runtime/command identity mismatch",
        )
        if index:
            require(spec.model_dump(mode="json") == plan["workload"], "workload changed")
            require(
                variant == RuntimeVariant.model_validate(report["contracts"]["variant"])
                and cap == CapabilitySnapshot.model_validate(report["contracts"]["capability"]),
                "qualified execution binding changed",
            )
            require(
                not compatibility(spec, candidate, variant, cap, at=stamp(row["created_at"])),
                "incompatible execution",
            )
        else:
            require(
                signature(spec) == plan["source_workload_digest"], "qualification source mismatch"
            )
            require(
                "CAPABILITY_STALE" in row["submission_warnings"],
                "historical capability warning hidden",
            )
        require(
            variant.workload_ref == spec.ref
            and variant.workload_signature == signature(spec.identity)
            and variant.model_digest == spec.identity.model_digest,
            "workload/model identity mismatch",
        )
        result = ExecutionResult.model_validate(row["result"])
        require(
            signature(result) == row["result_digest"]
            and result.job_id == row["job_id"]
            and result.attempt_id == ref
            and result.workload_signature == signature(spec.identity)
            and result.context_signature == context_signature(candidate, variant),
            "result digest/identity mismatch",
        )
        require(
            row["state"] == "SUCCEEDED"
            and result.evidence_kind == "hardware"
            and result.outcome == "COMPLETED"
            and result.measurements.quality_value >= spec.quality.minimum
            and result.measurements.peak_memory_mib <= spec.quality.maximum_peak_memory_mib
            and result.measurements.work_units == spec.identity.work_units,
            "invalid quality/result",
        )
        numerical = row["numerical_qualification"]
        require(
            numerical["result"] == "PASS"
            and numerical["compared_elements"] == 400
            and numerical["measured_forwards"] == spec.identity.work_units
            and len(numerical["samples_seconds"]) == spec.identity.work_units
            and all(math.isfinite(x) and x > 0 for x in numerical["samples_seconds"])
            and math.isclose(sum(numerical["samples_seconds"]), result.measurements.elapsed_seconds)
            and numerical["cpu_fallback"] is False
            and "sm_87" in numerical["cuda_arch_list"]
            and numerical["torch"] == variant.runtime_versions["pytorch"]
            and numerical["cuda"] == variant.runtime_versions["cuda"]
            and "sha256:" + numerical["weights_sha256"] == spec.identity.model_digest
            and numerical["input_sha256"] in spec.identity.dataset_version
            and tuple(numerical["input_shape"]) == spec.identity.input_shape,
            "native numerical/model/runtime evidence mismatch",
        )
        boundary = numerical["enforced_boundary"]
        require(
            boundary["memory_max_bytes"] == 1024 * 2**20
            and 0 < boundary["memory_peak_bytes"] <= boundary["memory_max_bytes"]
            and boundary["swap_max_bytes"] == 0
            and boundary["cpu_affinity_count"] == 1,
            "native cgroup resource boundary mismatch",
        )
        ledger = row["ledger"]
        require(
            ledger["attempt_id"] == ref
            and ledger["project"] == spec.project_ref
            and ledger["backend"] == "slurm"
            and ledger["body"]["job_id"] == row["job_id"],
            "accounting ownership mismatch",
        )
        records = native_rows(row["native_accounting"])
        parents = [n for n in records if n["job_id"] == row["native_job_id"]]
        require(len(parents) == 1, "missing/ambiguous native parent")
        native = parents[0]
        require(
            native["state"] == "COMPLETED"
            and native["exit_code"] == "0:0"
            and native["job_name"] == ref
            and native["account"] == ledger["project"]
            and native["node"] == cap.node_ref,
            "native identity/outcome mismatch",
        )
        times = [stamp(native[k]) for k in ("submit", "start", "end")]
        require(all(times) and times == sorted(times), "native chronological mismatch")
        seconds = (times[2] - times[1]).total_seconds()
        require(seconds == float(native["elapsed_seconds"]) > 0, "native elapsed mismatch")
        require(seconds <= plan["budget"]["max_run_seconds"], "native run budget exceeded")
        require(
            (times[1] - times[0]).total_seconds() == ledger["queue_seconds"],
            "queue waiting cost mismatch",
        )
        tres = dict(v.split("=", 1) for v in native["alloc_tres"].split(","))
        require(
            tres.get("cpu") == "1" and tres.get("gres/gpu") == "1" and tres.get("mem") == "1G",
            "native reservation differs from frozen resources",
        )
        device_cost = cost(ledger["allocated_device_seconds"], "missing device cost")
        cpu_cost = cost(ledger["body"]["allocated_cpu_seconds"], "missing CPU reservation")
        require(device_cost == cpu_cost == seconds, "reservation duration mismatch")
        require(
            ledger["measured_compute_seconds"] == result.measurements.elapsed_seconds,
            "compute cost mismatch",
        )
        require(ledger["body"]["allocation_memory_mib"] == 1024, "memory reservation missing")
        gpu_seconds += device_cost
        cpu_seconds += cpu_cost
        memory_seconds += seconds * 1024
        profile = row["profile"]
        require(
            profile["result"] == row["result"] and profile["job_id"] == row["job_id"],
            "profile mismatch",
        )
        require(stamp(profile["recorded_at"]) >= times[2], "profile chronology mismatch")
        delivery = row["delivery"]
        require(
            delivery["matching_s3_api_mlflow_bytes"]
            and delivery["mlflow_runs"] == delivery["ledger_rows"] == 1
            and delivery["result_digest"] == row["result_digest"],
            "result publication evidence missing",
        )
    rec, approval = report["recommendation"], report["approval"]
    main = report["attempts"][-1]
    sources = report["attempts"][1:-1]
    source_refs = [s["attempt_id"] for s in sources]
    require(
        len(source_refs) == len(set(source_refs)) >= plan["workload"]["quality"]["minimum_repeats"],
        "insufficient independent repeats",
    )
    require(
        digest_body(rec) == rec["digest"] == approval["recommendation_digest"],
        "recommendation digest mismatch",
    )
    require(
        approval["recommendation_ref"] == rec["ref"]
        and main["request"]["approval_ref"] == approval["ref"]
        and main["request"]["mode"] == "fixed"
        and main["request"]["candidate_ref"] == rec["candidate_ref"],
        "explicit approval binding mismatch",
    )
    require(
        all(stamp(s["profile"]["recorded_at"]) <= stamp(rec["created_at"]) for s in sources)
        and stamp(rec["created_at"]) < stamp(main["created_at"]),
        "chronological source/target leakage",
    )
    rank = next(r for r in rec["ranking"] if r["candidate_ref"] == rec["candidate_ref"])
    require(set(rank["evidence_refs"]) == set(source_refs), "reference cohort mismatch")
    receipt = main["feedback"]
    require(
        receipt["status"] == "COMPARABLE"
        and receipt["constraint_passed"]
        and receipt["source_profile_refs"] == rank["evidence_refs"]
        and receipt["attempt_id"] not in receipt["source_profile_refs"]
        and receipt["attempt_id"] == receipt["usage_attempt_id"] == main["attempt_id"]
        and receipt["native_job_id"] == main["native_job_id"]
        and receipt["result_digest"] == main["result_digest"],
        "feedback/reused confirmation mismatch",
    )
    values = [s["result"]["measurements"]["elapsed_seconds"] for s in sources]
    mean = statistics.mean(values)
    radius = 3 * statistics.stdev(values) / math.sqrt(len(values))
    interval = [max(0, mean - radius), mean + radius]
    actual = main["result"]["measurements"]["elapsed_seconds"]
    require(
        math.isclose(rank["mean_seconds"], mean)
        and all(math.isclose(a, b) for a, b in zip(rank["interval_seconds"], interval, strict=True))
        and math.isclose(receipt["reference_mean_seconds"], mean)
        and math.isclose(receipt["actual_seconds"], actual)
        and math.isclose(receipt["residual_seconds"], actual - mean, abs_tol=1e-12)
        and math.isclose(receipt["relative_residual"], (actual - mean) / mean, abs_tol=1e-12),
        "feedback objective/uncertainty/residual mismatch",
    )
    next_rec = report["next_recommendation"]
    require(
        next_rec["measured"]
        and main["attempt_id"] in next_rec["ranking"][0]["evidence_refs"]
        and stamp(next_rec["created_at"]) >= stamp(main["profile"]["recorded_at"]),
        "actual result not incorporated into next recommendation",
    )
    protocol_wall = (stamp(report["completed_at"]) - stamp(report["started_at"])).total_seconds()
    require(
        0 < protocol_wall <= plan["budget"]["whole_protocol_wall_seconds"],
        "protocol wall budget exceeded",
    )
    require(
        gpu_seconds <= plan["budget"]["max_gpu_reservation_seconds"],
        "GPU reservation budget exceeded",
    )
    return {
        "schema_version": "right-sizing-slurm-audit-v1",
        "experiment_id": report["experiment_id"],
        "native_jobs": len(native_ids),
        "qualification_jobs": 1,
        "observe_jobs": len(sources),
        "independent_approved_jobs": 1,
        "gpu_reservation_seconds": gpu_seconds,
        "cpu_core_reservation_seconds": cpu_seconds,
        "host_memory_mib_seconds": memory_seconds,
        "protocol_wall_seconds": protocol_wall,
        "reference_mean_seconds": mean,
        "reference_interval_seconds": interval,
        "actual_seconds": actual,
        "relative_residual": (actual - mean) / mean,
        "actual_inside_descriptive_interval": interval[0] <= actual <= interval[1],
        "next_history_source_count": len(next_rec["ranking"][0]["evidence_refs"]),
        "closed_loop_verified": True,
        "performance_improvement_claim": False,
        "limitations": [
            "One fixed qualified candidate; not Slurm resource search or a cross-backend comparison.",
            "Forward-only timing excludes initialization, full native guard and scheduler reservation.",
            "Descriptive mean interval is not a calibrated predictive interval.",
            "Energy, actual model CPU time and reused environment preparation cost remain unknown.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(json.loads(args.report.read_text()), json.loads(args.plan.read_text()))
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write("\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
