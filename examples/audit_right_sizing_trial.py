"""Audit a captured trial without submitting Jobs or inventing missing measurements."""

import argparse
import json
import math
from pathlib import Path

from audit_hailo_platform import audit as audit_hailo_platform

from resource_advisor.contracts import Candidate, ExecutionResult, RuntimeVariant, signature
from resource_advisor.policy import context_signature
from resource_advisor.uncertainty import stamp


def require(value, reason):
    if not value:
        raise ValueError(reason)


def cost(value, label):
    require(isinstance(value, (int, float)) and math.isfinite(value) and value >= 0, label)
    return value


def audit_hailo_feedback(report, manifest):
    """Reuse the exact model/quality auditor and check the new closed-loop receipt."""
    result = audit_hailo_platform(report, manifest)
    qualification = report["qualification"]
    require(
        signature(qualification["result"]) == qualification["result_digest"],
        "qualification digest mismatch",
    )
    require(
        signature(qualification["report"]) == qualification["report_digest"],
        "qualification report mismatch",
    )
    require(
        qualification["job_uid"] not in {r["backend"]["uid"] for r in report["runs"]},
        "qualification reused",
    )
    require(
        not report["cold_start"]["recommendation"]["measured"], "unknown workload did not abstain"
    )
    row = report["runs"][3]
    receipt = row["feedback"]
    require(
        receipt["status"] == "COMPARABLE" and receipt["constraint_passed"],
        "feedback is not comparable",
    )
    require(
        receipt["attempt_id"] == row["result"]["attempt_id"]
        and receipt["result_digest"] == row["result_digest"],
        "feedback identity mismatch",
    )
    require(
        receipt["source_profile_refs"] == report["recommendation"]["ranking"][0]["evidence_refs"],
        "feedback provenance mismatch",
    )
    require(receipt["attempt_id"] not in receipt["source_profile_refs"], "reused confirmation")
    actual = row["result"]["measurements"]["elapsed_seconds"]
    expected = sum(r["result"]["measurements"]["elapsed_seconds"] for r in report["runs"][:3]) / 3
    require(
        abs(actual - receipt["actual_seconds"]) < 1e-12
        and abs(expected - receipt["reference_mean_seconds"]) < 1e-12,
        "feedback objective mismatch",
    )
    require(
        abs(actual - expected - receipt["residual_seconds"]) < 1e-12, "feedback residual mismatch"
    )
    for source in report["runs"][:3]:
        require(source["profile"]["result"] == source["result"], "source profile mismatch")
        require(
            stamp(source["profile"]["recorded_at"])
            <= stamp(report["recommendation"]["created_at"]),
            "future profile leakage",
        )
    return {
        **result,
        "feedback_status": receipt["status"],
        "relative_residual": receipt["relative_residual"],
        "total_npu_reservation_seconds": result["api_npu_reservation_seconds"]
        + result["qualification_npu_reservation_seconds"],
        "comparison_scope": "Same Hailo configuration feedback, not GPU/NPU performance comparison",
    }


def audit(report, plan):
    require(report["plan_digest"] == signature(plan), "frozen plan digest mismatch")
    require(report["experiment_id"] == plan["experiment_id"], "experiment ID mismatch")
    require(report["source_commit"] == plan["source_commit"], "source commit mismatch")
    attempts, native, profiles = {}, set(), {}
    for row in report["attempts"]:
        ref = row["attempt_id"]
        require(ref not in attempts, "duplicate attempt")
        attempts[ref] = row
        proof = row["native"]
        require(proof["job_uid"] and proof["job_uid"] not in native, "duplicate native Job")
        native.add(proof["job_uid"])
        require(proof["admitted"] and proof["quota_reserved"], "missing native admission")
        times = [stamp(row.get(k)) for k in ("submitted_at", "started_at", "finished_at")]
        require(all(times) and times == sorted(times), "missing/reversed native timing")
        reservation = cost(row["device_seconds"], "missing accelerator reservation")
        seconds = (times[2] - times[1]).total_seconds()
        count = row["candidate"]["context"]["resources"]["accelerator_count"]
        require(abs(reservation - seconds * count) < 1e-7, "reservation/timing mismatch")
        cpu = row["candidate"]["context"]["resources"]["host_cpu"]
        memory = row["candidate"]["context"]["resources"]["host_memory_mib"]
        require(
            abs(cost(row["cpu_core_seconds"], "missing CPU reservation") - cpu * seconds) < 1e-7,
            "CPU reservation mismatch",
        )
        require(
            abs(cost(row["memory_mib_seconds"], "missing memory reservation") - memory * seconds)
            < 1e-7,
            "memory reservation mismatch",
        )
        require(stamp(row["created_at"]) <= times[2], "execution predates request")
        cost(row["request_to_terminal_seconds"], "missing execution wall cost")
        result = row.get("result")
        if result:
            parsed = ExecutionResult.model_validate(result)
            require(signature(parsed) == row["result_digest"], "result digest mismatch")
            require(
                parsed.attempt_id == ref and parsed.job_id == row["job_id"], "result ID mismatch"
            )
            require(
                parsed.workload_signature == signature(row["spec"]["identity"]),
                "workload digest mismatch",
            )
            ctx = context_signature(
                Candidate.model_validate(row["candidate"]),
                RuntimeVariant.model_validate(row["variant"]),
            )
            require(parsed.context_signature == ctx, "execution context mismatch")
            require(parsed.evidence_kind == "hardware", "synthetic evidence in hardware trial")
            if row["state"] == "SUCCEEDED":
                q = row["spec"]["quality"]
                require(
                    parsed.measurements is not None
                    and parsed.measurements.quality_value >= q["minimum"]
                    and parsed.measurements.peak_memory_mib <= q["maximum_peak_memory_mib"],
                    "invalid quality result",
                )
        elif row["state"] == "SUCCEEDED":
            raise ValueError("successful Job has no result")
        if row.get("profile"):
            profile = row["profile"]
            require(
                profile["ref"] == ref and profile["body"]["result"] == result,
                "profile ownership mismatch",
            )
            profiles[ref] = profile
        feedback = row.get("feedback")
        if feedback:
            require(
                feedback["attempt_id"] == ref and feedback["job_id"] == row["job_id"],
                "feedback ID mismatch",
            )
            require(
                feedback["result_digest"] == row["result_digest"], "feedback result digest mismatch"
            )
            require(feedback["usage_attempt_id"] == ref, "feedback accounting ownership mismatch")
            require(ref not in feedback["source_profile_refs"], "reused confirmation/main result")
    studies = report["studies"]
    for entry in studies:
        study = entry["study"]
        spec = study["spec"]
        probe_ids, confirmation_ids = set(), set()
        for observation in study["observations"]:
            ref = observation["attempt_id"]
            require(ref in attempts, "missing study attempt/cost")
            row = attempts[ref]
            require(row["job_id"] == observation["job_id"], "study Job ID mismatch")
            require(row["candidate"]["ref"] == observation["candidate_ref"], "candidate mismatch")
            require(
                abs(row["device_seconds"] - observation["device_seconds"]) < 1e-7,
                "study cost mismatch",
            )
            require(stamp(row["created_at"]) >= stamp(study["created_at"]), "chronological leakage")
            (probe_ids if observation["mode"] == "pilot" else confirmation_ids).add(ref)
        require(not probe_ids & confirmation_ids, "reused confirmation")
        require(len(probe_ids) <= spec["profiling"]["max_probes"], "probe budget violation")
        for unit, charged in study["charged_device_seconds"].items():
            require(
                cost(charged, "missing charged budget")
                <= spec["profiling"]["device_seconds"][unit],
                "device budget violation",
            )
        for probe in entry["plans"]:
            cost(probe["body"]["choice"].get("planning_seconds"), "missing model/planning overhead")
        rec = study.get("recommendation")
        if rec and rec["measured"]:
            chosen = [
                o
                for o in study["observations"]
                if o["attempt_id"] in confirmation_ids
                and o["candidate_ref"] == rec["candidate_ref"]
                and o["outcome"] == "COMPLETED"
            ]
            require(
                len(chosen) >= spec["quality"]["minimum_repeats"],
                "insufficient independent confirmation",
            )
            require(
                stamp(rec["created_at"]) >= max(stamp(o["recorded_at"]) for o in chosen),
                "recommendation precedes confirmation",
            )
            for rank in rec["ranking"]:
                require(set(rank["evidence_refs"]) <= confirmation_ids, "training/target leakage")
        if entry["strategy"] in {"random", "qlognei"}:
            require(
                spec["profiling"]["max_probes"] == plan["gpu_search"]["max_probes"],
                "unequal probe budget",
            )
            require(
                spec["profiling"]["total_wall_seconds"] == plan["gpu_search"]["total_wall_seconds"],
                "unequal wall budget",
            )
    for row in attempts.values():
        feedback = row.get("feedback")
        if feedback and feedback["status"] == "COMPARABLE":
            sources = feedback["source_profile_refs"]
            require(
                len(sources) >= row["spec"]["quality"]["minimum_repeats"],
                "feedback insufficient repeats",
            )
            values = []
            for ref in sources:
                require(ref in profiles, "missing feedback source profile")
                source = profiles[ref]
                require(
                    stamp(source["body"]["recorded_at"])
                    <= stamp(row["recommendation"]["created_at"]),
                    "future profile leakage",
                )
                result = source["body"]["result"]
                require(
                    result["workload_signature"] == feedback["workload_signature"]
                    and result["context_signature"] == feedback["context_signature"],
                    "feedback source scope mismatch",
                )
                values.append(result["measurements"]["elapsed_seconds"])
            reference = sum(values) / len(values)
            actual = row["result"]["measurements"]["elapsed_seconds"]
            require(
                abs(reference - feedback["reference_mean_seconds"]) < 1e-9
                and abs(actual - reference - feedback["residual_seconds"]) < 1e-9,
                "feedback residual mismatch",
            )
    qualifications = report["qualifications"]
    for q in qualifications:
        require(q["native"]["job_uid"] not in native, "qualification reused as main/profile")
        native.add(q["native"]["job_uid"])
        cost(q["device_seconds"], "missing qualification cost")
        cost(q["cpu_core_seconds"], "missing qualification CPU cost")
        cost(q["wall_seconds"], "missing qualification wall cost")
        require(signature(q["result"]) == q["result_digest"], "qualification digest mismatch")
    curves = []
    for name in ("W1", "W2"):
        qualification = [q for q in qualifications if q["workload"] == name]
        for arm in ("static", "random", "qlognei"):
            main = [
                m
                for m in report["main"]
                if m["workload"] == name and m["arm"] == arm and m.get("attempt_id")
            ]
            setup = next(
                (s for s in studies if s["workload"] == name and s["strategy"] == arm), None
            )
            wall = sum(q["wall_seconds"] for q in qualification)
            device = sum(q["device_seconds"] for q in qualification)
            cpu = sum(q["cpu_core_seconds"] for q in qualification)
            if setup:
                wall += (
                    stamp(setup["terminal_observed_at"]) - stamp(setup["request_started_at"])
                ).total_seconds()
                for o in setup["study"]["observations"]:
                    a = attempts[o["attempt_id"]]
                    device += a["device_seconds"]
                    cpu += a["cpu_core_seconds"]
            for n, m in enumerate(sorted(main, key=lambda x: x["block"]), 1):
                a = attempts[m["attempt_id"]]
                wall += a["request_to_terminal_seconds"]
                device += a["device_seconds"]
                cpu += a["cpu_core_seconds"]
                curves.append(
                    {
                        "workload": name,
                        "arm": arm,
                        "N": n,
                        "wall_seconds": wall,
                        "device_seconds": device,
                        "cpu_core_seconds": cpu,
                    }
                )
    return {
        "schema_version": "right-sizing-trial-audit-v1",
        "experiment_id": report["experiment_id"],
        "attempt_count": len(attempts),
        "qualification_count": len(qualifications),
        "total_device_seconds": sum(a["device_seconds"] for a in attempts.values())
        + sum(q["device_seconds"] for q in qualifications),
        "feedback_count": sum(bool(a.get("feedback")) for a in attempts.values()),
        "comparable_feedback_count": sum(
            a.get("feedback", {}).get("status") == "COMPARABLE" for a in attempts.values()
        ),
        "failures": sum(a["state"] != "SUCCEEDED" for a in attempts.values()),
        "cumulative_measured_cost": curves,
        "overall_goal_complete": False,
        "limitations": [
            "No extrapolation beyond actual main runs; setup includes confirmation and model/control overhead once.",
            "Qualification is charged equally to each arm; historical image construction and energy are unmeasured.",
            "Wall and device reservation are separate objectives; mixed GPU/NPU totals are accounting counts, not equivalent economic costs.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report, plan = json.loads(args.report.read_text()), json.loads(args.plan.read_text())
    result = (
        audit_hailo_feedback(report, plan)
        if report.get("kind") == "hailo-platform-integration"
        else audit(report, plan)
    )
    rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered)
