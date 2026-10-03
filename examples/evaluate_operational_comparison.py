"""Audit a completed, prospectively allocated B0/B1/B2 hardware comparison."""

import argparse
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from resource_advisor.contracts import ExecutionResult, WorkloadSpec, signature
from resource_advisor.diagnostics import validate_profile
from resource_advisor.thermal import assess, validate_trace


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def stamp(value):
    value = datetime.fromisoformat(value)
    require(value.tzinfo is not None, "timezone-aware timestamp required")
    return value


def elapsed(start, end):
    value = (stamp(end) - stamp(start)).total_seconds()
    require(value >= 0, "reversed timing boundary")
    return value


def number(value, positive=False):
    return (
        type(value) in (float, int)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0)
    )


def result_audit(row, spec, thermal_policy):
    result = row["result"]
    m, q = result["measurements"], spec["quality"]
    require(
        signature(result) == row["result_digest"]
        and result["attempt_id"] == row["attempt_id"]
        and result["job_id"] == row["job_id"]
        and result["workload_signature"] == signature(spec["identity"])
        and result["evidence_kind"] == "hardware"
        and result["measured"] is True
        and result["outcome"] == "COMPLETED"
        and row["measurements"] == m,
        "hardware result provenance mismatch",
    )
    require(
        number(m["elapsed_seconds"], True)
        and number(m["peak_memory_mib"])
        and number(m["quality_value"])
        and m["quality_value"] >= q["minimum"]
        and m["peak_memory_mib"] <= q["maximum_peak_memory_mib"]
        and m["work_units"] == spec["identity"]["work_units"]
        and row["thermal_assessment"]["status"] == "ELIGIBLE_TRACE",
        "infeasible result retained; complete successful comparison unavailable",
    )
    fixture = row["fixture"]
    require(
        fixture["model_weights_digest"] == spec["identity"]["model_digest"]
        and fixture["shape"] == spec["identity"]["input_shape"]
        and fixture["seed"] == spec["identity"]["seed"]
        and fixture["blocks"] == spec["identity"]["work_units"]
        and fixture["forwards_per_block"] == 32,
        "workload fixture changed",
    )
    parsed = ExecutionResult.model_validate(result)
    body = {
        "spec": spec,
        "candidate": next(c for c in spec["candidates"] if c["ref"] == row["candidate_ref"]),
        "variant": {"thermal_policy": thermal_policy},
    }
    trace = validate_trace(row["thermal_trace"], parsed, body)
    require(
        assess(trace, thermal_policy) == row["thermal_assessment"], "thermal classification changed"
    )
    validate_profile(row["phase_profile"], parsed, spec["identity"]["measurement_boundary"])


def summarize(report, plan):
    require(report["phase"] == "completed", "incomplete trial cannot produce final comparison")
    require(
        report["plan"] == plan and report["plan_digest"] == signature(plan), "frozen plan changed"
    )
    # Direct manifests may omit defaults that the API persists explicitly.
    spec = WorkloadSpec.model_validate(report["workload"]).model_dump(mode="json")
    profiling = report["profiling"]
    study = profiling["study"]
    require(
        study["state"] == "COMPLETED", "profiling abstention/failure must remain a separate outcome"
    )
    require(study["spec"] == spec, "profiling/main workload mismatch")
    require(
        study["request"]["strategy"] == plan["profiling"]["strategy"]
        and study["request"]["seed"] == plan["profiling"]["seed"],
        "profiling policy changed",
    )
    require(spec["identity"]["seed"] == plan["workload"]["seed"], "workload seed changed")
    require(
        spec["identity"]["input_shape"] == plan["workload"]["input_shape"], "workload shape changed"
    )
    require(spec["baseline_candidate_ref"] == "cpu1", "reasonable baseline changed")
    candidate_cpus = {c["ref"]: c["context"]["resources"]["host_cpu"] for c in spec["candidates"]}
    require(
        sorted(candidate_cpus.values()) == plan["workload"]["candidate_cpu_cores"],
        "candidate space changed",
    )
    rows = report["main"]
    require(
        len(rows) == len(plan["schedule"]) == plan["budgets"]["main_jobs"],
        "missing main observations",
    )
    profile_rows = profiling["observations"]
    all_rows = profile_rows + rows
    for key in ("job_id", "attempt_id", "backend_job_id"):
        require(len({r[key] for r in all_rows}) == len(all_rows), f"duplicate {key}")
    require(
        {r["attempt_id"] for r in profile_rows} == {r["attempt_id"] for r in study["observations"]},
        "profiling evidence missing or enlarged",
    )
    for row in all_rows:
        result_audit(row, spec, report["thermal_policy"])
    for candidate in candidate_cpus:
        require(
            len(
                {
                    r["result"]["context_signature"]
                    for r in all_rows
                    if r["candidate_ref"] == candidate
                }
            )
            == 1,
            "same candidate has different execution contexts",
        )
    plans = {p["ref"]: p for p in profiling["plans"]}
    require(
        len(plans) == len(profiling["plans"]) and set(plans) == set(study["plans"]),
        "plan cohort changed",
    )
    models, fallbacks = 0, 0
    for p in plans.values():
        allowed = {
            r["attempt_id"]
            for r in profile_rows
            if r["mode"] == "pilot" and stamp(r["recorded_at"]) < stamp(p["created_at"])
        }
        choice = p["choice"]
        require(
            set(choice.get("surrogate", {}).get("training_run_ids", [])) <= allowed,
            "main/future data leaked into tuning",
        )
        require(
            not choice.get("surrogate", {}).get("source_run_ids"),
            "external transfer history not approved",
        )
        models += choice.get("reason") == "CONSTRAINED_QLOGNEI"
        fallbacks += "FALLBACK" in str(choice.get("reason", ""))
    require(
        models == profiling["model_choices"] and fallbacks == profiling["fallback_choices"],
        "model provenance counts differ",
    )
    require(models > 0 and fallbacks == 0, "actual qLogNEI required; preserve fallback separately")
    rec, approval = report["recommendation"], report["approval"]
    require(rec == study["recommendation"] and rec["measured"], "confirmed recommendation changed")
    selected = rec["confirmed_candidate"]
    require(report["selected_candidate"] == selected, "selected candidate changed")
    require(
        approval["recommendation_ref"] == rec["ref"]
        and approval["recommendation_digest"] == signature(rec)
        and approval["candidate_ref"] == selected
        and approval["workload_digest"] == signature(spec),
        "approval references another recommendation",
    )
    confirmations = [r for r in profile_rows if r["mode"] == "confirmation"]
    require(
        set(rec["confirmation_run_ids"]) == {r["attempt_id"] for r in confirmations}
        and all(
            sum(r["candidate_ref"] == ref for r in confirmations)
            >= plan["profiling"]["minimum_repeats"]
            for ref in {selected, spec["baseline_candidate_ref"]}
        ),
        "independent confirmation evidence missing",
    )
    setup_wall = elapsed(profiling["request_started_at"], profiling["approval_finished_at"])
    require(
        all(number(r["allocated_device_seconds"]) for r in profile_rows),
        "unknown profiling allocation",
    )
    profile_gpu = math.fsum(r["allocated_device_seconds"] for r in profile_rows)
    profile_cpu = math.fsum(
        candidate_cpus[r["candidate_ref"]] * r["allocated_device_seconds"] for r in profile_rows
    )
    require(profile_gpu <= plan["profiling"]["physical_gpu_seconds"], "profiling GPU cap exceeded")
    require(
        study["recommendation"]["cost"]["wall_seconds"] <= plan["profiling"]["total_wall_seconds"],
        "profiling wall cap exceeded",
    )
    require(
        sum(r["mode"] == "pilot" for r in profile_rows) <= plan["profiling"]["max_probes"],
        "probe cap exceeded",
    )
    timings = []
    for index, (row, slot) in enumerate(zip(rows, plan["schedule"], strict=True)):
        require(
            row["index"] == index and {k: row[k] for k in slot} == slot,
            "randomized schedule changed",
        )
        require(
            stamp(profiling["approval_finished_at"]) < stamp(row["request_started_at"]),
            "main run preceded frozen recommendation",
        )
        if index:
            previous_end = (
                rows[index - 1].get("delivery_observed_at")
                or rows[index - 1]["raw_result_observed_at"]
            )
            require(stamp(previous_end) <= stamp(row["request_started_at"]), "serial arms overlap")
        expected = selected if row["arm"] == "B2" else "cpu1"
        require(
            row["mode"] == {"B0": "direct", "B1": "observe", "B2": "fixed"}[row["arm"]]
            and row["approval_ref"] == (approval["ref"] if row["arm"] == "B2" else None),
            "arm execution/approval path changed",
        )
        if row["arm"] == "B2":
            require(
                stamp(row["request_started_at"]) < stamp(approval["expires_at"]), "approval expired"
            )
        require(
            row["candidate_ref"] == expected and row["host_cpu"] == candidate_cpus[expected],
            "arm resource selection changed",
        )
        require(
            row["host_memory_mib"] == 2048 and row["physical_gpu_count"] == 1,
            "allocation units changed",
        )
        require(
            all(
                row["backend_verification"][k] is True
                for k in (
                    "kueue_admitted",
                    "same_queue",
                    "single_successful_pod",
                    "same_digest_pinned_image",
                )
            ),
            "backend comparability missing",
        )
        reservation = elapsed(row["scheduled_at"], row["container_finished_at"])
        require(
            number(row["gpu_reservation_seconds"])
            and reservation == row["gpu_reservation_seconds"],
            "reservation boundary mismatch",
        )
        require(
            row["cpu_core_reservation_seconds"] == reservation * row["host_cpu"]
            and row["memory_mib_reservation_seconds"] == reservation * 2048,
            "resource units conflated",
        )
        require(
            row["queue_seconds"] == elapsed(row["backend_submitted_at"], row["scheduled_at"]),
            "queue boundary mismatch",
        )
        require(
            row["preparation_seconds"] == elapsed(row["scheduled_at"], row["container_started_at"]),
            "preparation boundary mismatch",
        )
        require(
            row["container_seconds"]
            == elapsed(row["container_started_at"], row["container_finished_at"]),
            "container boundary mismatch",
        )
        require(
            stamp(row["container_finished_at"]) <= stamp(row["raw_result_observed_at"]),
            "result observed before completion",
        )
        raw = elapsed(row["request_started_at"], row["raw_result_observed_at"])
        validated = delivered = None
        recorded_validation = recorded_delivery = None
        if row["arm"] == "B0":
            require(
                row["delivery"]["platform_record"] is False
                and row["delivery"]["raw_log_verified"] is True,
                "B0 silently acquired platform recording",
            )
            require(
                row["validated_result_observed_at"] is None and row["delivery_observed_at"] is None,
                "B0 platform timing fabricated",
            )
        else:
            require(
                row["delivery"]["s3_api_mlflow_bytes_match"] is True
                and row["delivery"]["mlflow_state"] == "FINISHED",
                "linked delivery not verified",
            )
            validated = elapsed(row["request_started_at"], row["validated_result_observed_at"])
            delivered = elapsed(row["request_started_at"], row["delivery_observed_at"])
            require(raw <= validated <= delivered, "observed platform completion order changed")
            recorded_validation = elapsed(
                row["platform_job_created_at"], row["platform_result_recorded_at"]
            )
            recorded_delivery = elapsed(
                row["platform_job_created_at"], row["platform_delivery_recorded_at"]
            )
            require(
                recorded_validation <= recorded_delivery,
                "recorded platform completion order changed",
            )
        timings.append(
            {
                "index": index,
                "block": row["block"],
                "arm": row["arm"],
                "candidate_ref": expected,
                "raw_result_wall_seconds": raw,
                "validated_wall_seconds": validated,
                "delivery_wall_seconds": delivered,
                "platform_recorded_validation_seconds": recorded_validation,
                "platform_recorded_delivery_seconds": recorded_delivery,
                "compute_seconds": row["measurements"]["elapsed_seconds"],
                "gpu_seconds": reservation,
                "cpu_core_seconds": row["cpu_core_reservation_seconds"],
            }
        )
    require(
        len({r["backend_verification"]["normal_priority_class"] for r in rows}) == 1,
        "arm priority changed",
    )
    require(
        all(
            number(q["gpu_reservation_seconds"]) and q["exit_code"] == 0
            for q in report["qualification"]
        ),
        "unknown or failed qualification",
    )
    qualification_gpu = math.fsum(q["gpu_reservation_seconds"] for q in report["qualification"])
    require(
        len({q["job_ref"] for q in report["qualification"]}) == len(report["qualification"])
        and sorted(q["host_cpu"] for q in report["qualification"])
        == plan["workload"]["candidate_cpu_cores"],
        "qualification candidate coverage changed",
    )
    require(
        len(report["qualification"]) == plan["budgets"]["qualification_jobs"],
        "qualification cohort changed",
    )
    total_gpu = qualification_gpu + profile_gpu + math.fsum(t["gpu_seconds"] for t in timings)
    require(
        total_gpu
        == report["whole_protocol_gpu_seconds"]
        <= plan["budgets"]["whole_protocol_physical_gpu_seconds"],
        "global GPU cost mismatch/cap",
    )
    whole_wall = elapsed(report["protocol_started_at"], report["completed_at"])
    require(whole_wall <= plan["budgets"]["whole_protocol_wall_seconds"], "global wall cap")
    require(
        len(all_rows) + len(report["qualification"]) <= plan["budgets"]["total_jobs_maximum"],
        "job count cap",
    )
    arms, cumulative, paired = {}, [], []
    for arm in ("B0", "B1", "B2"):
        group = [t for t in timings if t["arm"] == arm]
        arms[arm] = {
            "count": len(group),
            "raw_mean_seconds": statistics.mean(t["raw_result_wall_seconds"] for t in group),
            "compute_mean_seconds": statistics.mean(t["compute_seconds"] for t in group),
            "gpu_seconds": math.fsum(t["gpu_seconds"] for t in group),
            "cpu_core_seconds": math.fsum(t["cpu_core_seconds"] for t in group),
        }
        # Explicit fsum keeps persisted totals stable across Python's sum implementations.
        for n in range(1, len(group) + 1):
            cumulative.append(
                {
                    "arm": arm,
                    "uses": n,
                    "raw_wall_seconds_including_profile": (setup_wall if arm == "B2" else 0)
                    + math.fsum(t["raw_result_wall_seconds"] for t in group[:n]),
                    "observed_completion_seconds_including_profile": (
                        setup_wall if arm == "B2" else 0
                    )
                    + math.fsum(
                        t["raw_result_wall_seconds"] if arm == "B0" else t["delivery_wall_seconds"]
                        for t in group[:n]
                    ),
                    "platform_recorded_delivery_seconds_including_profile": (
                        (setup_wall if arm == "B2" else 0)
                        + math.fsum(t["platform_recorded_delivery_seconds"] for t in group[:n])
                        if arm != "B0"
                        else None
                    ),
                    "gpu_seconds_including_profile": (profile_gpu if arm == "B2" else 0)
                    + math.fsum(t["gpu_seconds"] for t in group[:n]),
                    "cpu_core_seconds_including_profile": (profile_cpu if arm == "B2" else 0)
                    + math.fsum(t["cpu_core_seconds"] for t in group[:n]),
                }
            )
    for block in range(plan["blocks"]):
        group = {t["arm"]: t for t in timings if t["block"] == block}
        paired.append(
            {
                "block": block,
                "B1_minus_B0_raw_seconds": group["B1"]["raw_result_wall_seconds"]
                - group["B0"]["raw_result_wall_seconds"],
                "B2_minus_B1_raw_seconds": group["B2"]["raw_result_wall_seconds"]
                - group["B1"]["raw_result_wall_seconds"],
                "B2_minus_B1_compute_seconds": group["B2"]["compute_seconds"]
                - group["B1"]["compute_seconds"],
            }
        )
    return {
        "scope": "one generated CNN/GPU; descriptive blocked operational comparison",
        "source_capture_digest": signature(report),
        "selected_candidate": selected,
        "actual_qlognei_choices": models,
        "random_fallbacks": fallbacks,
        "profiling_jobs": len(profile_rows),
        "main_jobs": len(rows),
        "qualification_jobs": len(report["qualification"]),
        "whole_protocol_wall_seconds": whole_wall,
        "whole_protocol_gpu_seconds": total_gpu,
        "shared_qualification_gpu_seconds": qualification_gpu,
        "B2_setup_wall_seconds": setup_wall,
        "profiling_gpu_seconds": profile_gpu,
        "profiling_cpu_core_seconds": profile_cpu,
        "arms": arms,
        "main_timings": timings,
        "paired_differences": paired,
        "actual_serial_cumulative": cumulative,
        "limits": plan["limits"],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = summarize(json.loads(a.capture.read_text()), json.loads(a.plan.read_text()))
    with a.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
