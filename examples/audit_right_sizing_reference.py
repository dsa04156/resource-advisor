"""Audit an additive reference experiment; never repair failed measurements."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_right_sizing_trial import compare_reference, cost, require

from resource_advisor.contracts import signature
from resource_advisor.uncertainty import stamp


def audit(primary, primary_plan, reference, plan):
    names = tuple(c["name"] for c in plan["contracts"])
    comparison = compare_reference(primary, primary_plan, reference, plan, workloads=names)
    require(reference["source_digest"] == plan["source_digest"], "benchmark source mismatch")
    require(
        reference["controller_source_commit"] == plan["controller_source_commit"],
        "controller source mismatch",
    )
    started, finished = (
        stamp(reference["protocol_started_at"]),
        stamp(reference["protocol_finished_at"]),
    )
    require(started and finished and started <= finished, "missing protocol timing")
    wall = (finished - started).total_seconds()
    require(wall <= plan["whole_protocol_wall_seconds"], "whole protocol budget violation")
    require(started > stamp(plan["declared_at"]), "undeclared qualification")
    qualifications = reference["qualifications"]
    primary_uids = {a["native"]["job_uid"] for a in primary["attempts"] + primary["qualifications"]}
    require(
        not primary_uids & {a["native"]["job_uid"] for a in reference["attempts"] + qualifications},
        "reused primary qualification/native Job",
    )
    expected = {
        ref for c in plan["contracts"] for v in c["variants"] for ref in v["validation_refs"]
    }
    require(
        {q["native"]["job_ref"] for q in qualifications} == expected,
        "missing/extra fresh qualification",
    )
    require(len(qualifications) == len(expected), "duplicate qualification")
    for q in qualifications:
        spec = next(c["workload"] for c in plan["contracts"] if c["name"] == q["workload"])
        proof = q["native"]
        require(
            q["result"]["job_id"] == proof["job_ref"]
            and q["result"]["attempt_id"] == proof["job_ref"] + "-attempt",
            "qualification native/result identity mismatch",
        )
        variant = next(c["variants"][0] for c in plan["contracts"] if c["name"] == q["workload"])
        require(
            proof["image_digest"] == variant["image"].split("@")[-1],
            "qualification native image mismatch",
        )
        times = [stamp(proof[k]) for k in ("created_at", "scheduled_at", "container_finished_at")]
        require(all(times) and times == sorted(times), "qualification timing mismatch")
        require(started <= times[0] <= times[-1] <= finished, "qualification outside protocol")
        require(proof["admitted"] and proof["quota_reserved"], "qualification admission missing")
        seconds = (times[-1] - times[1]).total_seconds()
        require(abs(seconds - q["device_seconds"]) < 1e-7, "qualification reservation mismatch")
        require(
            abs((times[-1] - times[0]).total_seconds() - q["wall_seconds"]) < 1e-7,
            "qualification wall cost mismatch",
        )
        context = q["runtime_context"]
        require(
            context in [c["context"] for c in spec["candidates"]]
            and signature(spec["identity"]) == q["result"]["workload_signature"],
            "qualification workload/context mismatch",
        )
        resources = context["resources"]
        require(
            abs(seconds * resources["host_cpu"] - q["cpu_core_seconds"]) < 1e-7
            and abs(seconds * resources["host_memory_mib"] - q["memory_mib_seconds"]) < 1e-7,
            "qualification host cost mismatch",
        )
        measurements = q["result"]["measurements"]
        require(
            q["result"]["evidence_kind"] == "hardware"
            and measurements["quality_value"] >= spec["quality"]["minimum"]
            and measurements["peak_memory_mib"] <= spec["quality"]["maximum_peak_memory_mib"],
            "invalid qualification quality",
        )
    attempts = {a["attempt_id"]: a for a in reference["attempts"]}
    for entry in reference["studies"]:
        study = entry["study"]
        require(
            max(stamp(q["native"]["container_finished_at"]) for q in qualifications)
            <= stamp(entry["request_started_at"]),
            "qualification/target chronology leakage",
        )
        require(
            (
                stamp(entry["terminal_observed_at"]) - stamp(entry["request_started_at"])
            ).total_seconds()
            <= study["spec"]["profiling"]["total_wall_seconds"],
            "study wall budget violation",
        )
        plans = {p["ref"]: p["body"] for p in entry["plans"]}
        for observation in study["observations"]:
            row = attempts[observation["attempt_id"]]
            require(observation["plan_ref"] in plans, "observation missing immutable probe plan")
            probe = plans[observation["plan_ref"]]
            require(
                probe["candidate_ref"] == observation["candidate_ref"]
                and probe["mode"] == observation["mode"],
                "observation/plan scope mismatch",
            )
            frozen = next(
                c for c in plan["contracts"] if c["workload"]["ref"] == row["spec"]["ref"]
            )
            require(row["variant"] in frozen["variants"], "runtime variant changed")
            require(
                row["native"]["active_deadline_seconds"]
                == probe["execution_limits"]["max_run_seconds"],
                "native deadline differs from approved limit",
            )
            require(
                row["native"]["image_digest"] == row["variant"]["image"].split("@")[-1],
                "native image digest mismatch",
            )
        assessment = study.get("native_startup_assessment")
        if assessment:
            confirmation_plans = [
                p["body"] for p in entry["plans"] if p["body"]["mode"] == "confirmation"
            ]
            stopped_before_submission = (
                study.get("stop_reason") == "INSUFFICIENT_NATIVE_STARTUP_BUDGET"
            )
            if stopped_before_submission:
                confirmed = sum(o["mode"] == "confirmation" for o in study["observations"])
                target = study["confirmation_schedule"][confirmed]
                require(study["state"] == "ABSTAINED", "startup rejection is not terminal")
                require(
                    0
                    < assessment["native_active_deadline_seconds"]
                    <= study["spec"]["profiling"]["max_wall_seconds_per_candidate"],
                    "startup rejected cap exceeds consent",
                )
            else:
                require(confirmation_plans, "startup assessment missing confirmation plan")
                latest = max(confirmation_plans, key=lambda p: stamp(p["created_at"]))
                target = latest["candidate_ref"]
                require(
                    assessment["native_active_deadline_seconds"]
                    == latest["execution_limits"]["max_run_seconds"],
                    "startup assessment/plan deadline mismatch",
                )
            for source in assessment["source_attempts"]:
                row = attempts.get(source["attempt_id"])
                require(row and row["state"] == "SUCCEEDED", "startup used unmeasured attempt")
                require(
                    any(
                        o["attempt_id"] == row["attempt_id"]
                        and o["mode"] == "pilot"
                        and o["candidate_ref"] == target
                        for o in study["observations"]
                    ),
                    "startup confirmation leakage",
                )
                actual = (
                    stamp(row["execution_started_at"]) - stamp(row["submitted_at"])
                ).total_seconds()
                require(
                    abs(actual - source["startup_upper_observation_seconds"]) < 1e-7,
                    "startup evidence mismatch",
                )
            maximum = max(
                (s["startup_upper_observation_seconds"] for s in assessment["source_attempts"]),
                default=None,
            )
            require(
                maximum == assessment["maximum_observed_startup_seconds"],
                "startup aggregate mismatch",
            )
            expected_status = (
                "UNKNOWN_STARTUP"
                if maximum is None
                else "INSUFFICIENT_NATIVE_STARTUP_BUDGET"
                if maximum >= assessment["native_active_deadline_seconds"]
                else "OBSERVED_STARTUP_FITS_BOUND"
            )
            require(assessment["status"] == expected_status, "startup verdict mismatch")
    all_rows = list(attempts.values()) + qualifications
    return {
        **comparison,
        "schema_version": "right-sizing-reference-audit-v3",
        "fresh_native_jobs": len(all_rows),
        "fresh_qualification_jobs": len(qualifications),
        "protocol_wall_seconds": wall,
        "cpu_core_reservation_seconds": sum(
            cost(r["cpu_core_seconds"], "CPU cost") for r in all_rows
        ),
        "memory_mib_reservation_seconds": sum(
            cost(r["memory_mib_seconds"], "memory cost") for r in all_rows
        ),
        "startup_guard": [
            s["study"].get("native_startup_assessment") for s in reference["studies"]
        ],
        "reference_day_scope": "Later-day descriptive reference; neither a contemporaneous oracle nor evidence of causal optimizer advantage.",
        "overall_goal_complete": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("primary", type=Path)
    parser.add_argument("primary_plan", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = (args.primary, args.primary_plan, args.reference, args.plan)
    result = audit(*(json.loads(p.read_text()) for p in paths))
    result["inputs"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered)
