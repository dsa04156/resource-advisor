"""Audit a captured trial without submitting Jobs or inventing missing measurements."""

import argparse
import json
import math
from pathlib import Path

from audit_hailo_platform import audit as audit_hailo_platform

from resource_advisor.contracts import (
    Candidate,
    ExecutionResult,
    RuntimeVariant,
    WorkloadSpec,
    signature,
)
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
    for check in report.get("stale_reuse", []):
        require(stamp(check["expires_at"]) < stamp(check["checked_at"]), "premature expiry check")
        require(
            not check["validity"]["reusable"] and check["submission_http_status"] == 422,
            "stale recommendation was accepted",
        )
        require(
            check["new_jobs"] == 0 and check["same_idempotency_key_has_no_row"],
            "stale rejection created a Job",
        )
    attempts, native, profiles, job_ids = {}, set(), {}, set()
    for row in report["attempts"]:
        ref = row["attempt_id"]
        require(ref not in attempts, "duplicate attempt")
        attempts[ref] = row
        require(row["job_id"] not in job_ids, "duplicate platform Job")
        job_ids.add(row["job_id"])
        spec = WorkloadSpec.model_validate(row["spec"])
        candidate = Candidate.model_validate(row["candidate"])
        variant = RuntimeVariant.model_validate(row["variant"])
        require(
            candidate in spec.candidates and variant.workload_ref == spec.ref,
            "candidate/variant differs from submitted workload",
        )
        require(
            variant.workload_signature == signature(spec.identity)
            and variant.model_digest == spec.identity.model_digest,
            "runtime/workload digest mismatch",
        )
        if "workloads" in plan:
            name = "W1" if "-w1" in spec.ref else "W2" if "-w2" in spec.ref else None
            require(name is not None, "unknown trial workload")
            frozen = plan["workloads"][name]
            require(
                spec.identity.code_digest == plan["source_digest"]
                and spec.identity.model_digest == frozen["model_digest"]
                and list(spec.identity.input_shape) == frozen["shape"]
                and spec.identity.work_units == frozen["work_units"]
                and spec.identity.precision == frozen["precision"]
                and spec.identity.seed == frozen["seed"]
                and spec.quality.minimum == frozen["quality_minimum"],
                "workload/quality differs from frozen plan",
            )
        else:
            frozen = next(
                (x["workload"] for x in plan["contracts"] if x["workload"]["ref"] == spec.ref), None
            )
            require(
                frozen is not None and spec == WorkloadSpec.model_validate(frozen),
                "reference contract differs from frozen plan",
            )
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
            body = probe["body"]
            require(
                body["study_ref"] == study["ref"] and body["workload_digest"] == signature(spec),
                "probe plan scope mismatch",
            )
            surrogate = body["choice"].get("surrogate")
            if surrogate:
                training = surrogate["training_run_ids"]
                require(
                    len(training) == len(set(training)) and set(training) <= probe_ids,
                    "training/target leakage or duplicate model observations",
                )
                for ref in training:
                    source = attempts[ref]
                    require(
                        source["state"] == "SUCCEEDED" and source["result"] is not None,
                        "model used unmeasured/failed observation",
                    )
                    require(
                        stamp(source["result_recorded_at"]) <= stamp(body["created_at"]),
                        "future model observation leakage",
                    )
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
        require(
            q["result"]["context_signature"] == signature(q["runtime_context"]),
            "qualification context mismatch",
        )
    maxima = plan["maximum_jobs"]
    allowed_gpu_jobs = sum(v for k, v in maxima.items() if k.startswith("gpu_"))
    require(
        len(attempts) + len(qualifications) <= allowed_gpu_jobs, "protocol Job budget violation"
    )
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
    main_summary = []
    for name in ("W1", "W2"):
        grouped = {}
        for m in report["main"]:
            if m["workload"] == name and m.get("attempt_id"):
                grouped.setdefault(m["arm"], []).append(attempts[m["attempt_id"]])
        baseline_values = [
            a["result"]["measurements"]["elapsed_seconds"]
            for a in grouped.get("static", [])
            if a["state"] == "SUCCEEDED"
        ]
        baseline_mean = sum(baseline_values) / len(baseline_values) if baseline_values else None
        for arm, rows in grouped.items():
            values = [
                a["result"]["measurements"]["elapsed_seconds"]
                for a in rows
                if a["state"] == "SUCCEEDED"
            ]
            mean = sum(values) / len(values) if values else None
            main_summary.append(
                {
                    "workload": name,
                    "arm": arm,
                    "attempt_ids": [a["attempt_id"] for a in rows],
                    "successful_jobs": len(values),
                    "failed_jobs": len(rows) - len(values),
                    "objective_seconds": values,
                    "mean_objective_seconds": mean,
                    "relative_to_static_mean": (mean - baseline_mean) / baseline_mean
                    if mean and baseline_mean
                    else None,
                }
            )
    net_benefit = []
    for name in ("W1", "W2"):
        for arm in ("random", "qlognei"):
            base = {p["N"]: p for p in curves if p["workload"] == name and p["arm"] == "static"}
            advisor = [p for p in curves if p["workload"] == name and p["arm"] == arm]
            failed = any(
                s["failed_jobs"]
                for s in main_summary
                if s["workload"] == name and s["arm"] in {"static", arm}
            )
            for p in advisor:
                if p["N"] not in base:
                    continue
                deltas = {
                    k: base[p["N"]][k] - p[k]
                    for k in ("wall_seconds", "device_seconds", "cpu_core_seconds")
                }
                net_benefit.append(
                    {
                        "workload": name,
                        "arm": arm,
                        "N": p["N"],
                        "baseline_minus_advisor": deltas,
                        "assessment": "INSUFFICIENT_EQUIVALENT_SUCCESS_EVIDENCE"
                        if failed
                        else "PROFILING_COST_NOT_RECOVERED_AT_OBSERVED_N"
                        if deltas["wall_seconds"] <= 0
                        else "LOWER_OBSERVED_LATENCY_COST_AT_THIS_N",
                        "semantics": "Descriptive actual finite-N cost assessment, not a price model or extrapolated break-even",
                    }
                )
    reservation_by_unit = {}
    for row in list(attempts.values()) + qualifications:
        unit = row["reservation_unit"]
        reservation_by_unit[unit] = reservation_by_unit.get(unit, 0) + row["device_seconds"]
    search = []
    for entry in studies:
        if entry["strategy"] not in {"random", "qlognei"}:
            continue
        study = entry["study"]
        plans = entry["plans"]
        probes = [o for o in study["observations"] if o["mode"] == "pilot"]
        confirmation = [o for o in study["observations"] if o["mode"] == "confirmation"]
        reference = next(
            (
                s
                for s in studies
                if s["workload"] == entry["workload"] and s["strategy"] == "grid_characterization"
            ),
            None,
        )
        ref_means = {}
        if reference:
            for o in reference["study"]["observations"]:
                if o["mode"] == "confirmation" and o["outcome"] == "COMPLETED":
                    ref_means.setdefault(o["candidate_ref"], []).append(
                        o["measurements"]["elapsed_seconds"]
                    )
            ref_means = {
                k: sum(v) / len(v)
                for k, v in ref_means.items()
                if len(v) >= study["spec"]["quality"]["minimum_repeats"]
            }
            require(
                stamp(reference["request_started_at"]) >= stamp(entry["terminal_observed_at"]),
                "characterization leaked into search",
            )
        rec = study.get("recommendation")
        selected = rec["candidate_ref"] if rec and rec["measured"] else None
        signed_difference = (
            (ref_means[selected] - min(ref_means.values())) / min(ref_means.values())
            if selected in ref_means
            else None
        )
        search.append(
            {
                "workload": entry["workload"],
                "strategy": entry["strategy"],
                "state": study["state"],
                "selected_candidate": selected,
                "probes": len(probes),
                "unique_probed_candidates": len({o["candidate_ref"] for o in probes}),
                "candidate_count": len(study["spec"]["candidates"]),
                "confirmation_jobs": len(confirmation),
                "planning_seconds": sum(p["body"]["choice"]["planning_seconds"] for p in plans),
                "actual_bo_choices": sum(
                    p["body"]["choice"]["reason"] == "CONSTRAINED_QLOGNEI" for p in plans
                ),
                "fallback_choices": sum("FALLBACK" in p["body"]["choice"]["reason"] for p in plans),
                "study_elapsed_seconds": (
                    stamp(entry["terminal_observed_at"]) - stamp(entry["request_started_at"])
                ).total_seconds(),
                "profile_confirmation_device_seconds": sum(
                    o["device_seconds"] for o in study["observations"]
                ),
                "later_reference_means_seconds": ref_means,
                "relative_distance_to_measured_reference": signed_difference,
                "reference_complete": bool(
                    reference
                    and reference["study"]["state"] == "COMPLETED"
                    and len(ref_means) == len(study["spec"]["candidates"])
                ),
                "abstained": selected is None,
            }
        )
    return {
        "schema_version": "right-sizing-trial-audit-v1",
        "experiment_id": report["experiment_id"],
        "attempt_count": len(attempts),
        "qualification_count": len(qualifications),
        "total_device_seconds": sum(a["device_seconds"] for a in attempts.values())
        + sum(q["device_seconds"] for q in qualifications),
        "reservation_by_unit": reservation_by_unit,
        "search_comparison": search,
        "main_execution": main_summary,
        "net_benefit": net_benefit,
        "feedback_count": sum(bool(a.get("feedback")) for a in attempts.values()),
        "comparable_feedback_count": sum(
            (a.get("feedback") or {}).get("status") == "COMPARABLE" for a in attempts.values()
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


def audit_phase_readback(supplement, report, report_sha256):
    from resource_advisor.diagnostics import diagnose, validate_profile
    from resource_advisor.thermal import assess, validate_trace

    require(supplement["primary_sha256"] == report_sha256, "phase primary digest mismatch")
    require(supplement["experiment_id"] == report["experiment_id"], "phase experiment mismatch")
    attempts = {a["attempt_id"]: a for a in report["attempts"]}
    expected = {m["attempt_id"]: m["arm"] for m in report["main"] if m["workload"] == "W2"}
    require(len(supplement["runs"]) == len(expected), "missing phase evidence")
    seen, grouped = set(), {}
    for row in supplement["runs"]:
        ref = row["attempt_id"]
        require(ref not in seen and expected.get(ref) == row["arm"], "phase identity mismatch")
        seen.add(ref)
        a = attempts[ref]
        result = ExecutionResult.model_validate(a["result"])
        profile = validate_profile(
            row["phase"], result, a["spec"]["identity"]["measurement_boundary"]
        )
        trace = validate_trace(row["thermal"], result, a)
        assessment = assess(trace, a["variant"]["thermal_policy"])
        require(assessment == row["thermal_assessment"], "thermal assessment mismatch")
        group = grouped.setdefault(
            row["arm"],
            {
                "attempt_ids": [],
                "phases_seconds": {},
                "objective_seconds": 0,
                "sensor_query_seconds": 0,
                "maximum_observed_temperature_c": 0,
                "diagnoses": [],
            },
        )
        group["attempt_ids"].append(ref)
        group["objective_seconds"] += result.measurements.elapsed_seconds
        group["sensor_query_seconds"] += assessment["sensor_query_seconds"]
        group["maximum_observed_temperature_c"] = max(
            group["maximum_observed_temperature_c"], assessment["maximum_observed_temperature_c"]
        )
        group["diagnoses"].append(
            diagnose(profile, evidence_kind=result.evidence_kind)["hypothesis"]
        )
        for sample in profile.samples:
            for phase, duration in sample.phases_seconds.items():
                group["phases_seconds"][phase] = group["phases_seconds"].get(phase, 0) + duration
    for group in grouped.values():
        group["phase_shares"] = {
            k: v / group["objective_seconds"] for k, v in group["phases_seconds"].items()
        }
        group["sensor_query_over_objective"] = (
            group["sensor_query_seconds"] / group["objective_seconds"]
        )
    return {
        "schema_version": "right-sizing-phase-audit-v1",
        "experiment_id": report["experiment_id"],
        "runs": len(seen),
        "by_arm": grouped,
        "scope": supplement["limitations"],
        "conclusion": "CPU preparation is a small measured fraction; dominant accelerator path includes launch/device/synchronization, not a causal hardware bottleneck verdict.",
    }


def compare_reference(report, plan, reference, reference_plan):
    """Join an immutable later characterization without leaking it into selection."""
    primary, later = audit(report, plan), audit(reference, reference_plan)
    require(
        reference["parent_experiment_id"] == report["experiment_id"], "reference parent mismatch"
    )
    require(
        reference_plan["parent_experiment_id"] == report["experiment_id"],
        "reference plan parent mismatch",
    )
    require(
        reference["source_commit"] == report["source_commit"]
        and reference_plan["source_digest"] == plan["source_digest"],
        "reference source mismatch",
    )
    require(
        not (
            {a["attempt_id"] for a in report["attempts"]}
            & {a["attempt_id"] for a in reference["attempts"]}
        ),
        "reused reference attempt",
    )
    require(
        not (
            {a["native"]["job_uid"] for a in report["attempts"]}
            & {a["native"]["job_uid"] for a in reference["attempts"]}
        ),
        "reused reference Job",
    )
    selections = [s for s in report["studies"] if s["strategy"] in {"random", "qlognei"}]
    last_main = max(
        stamp(a["result_recorded_at"])
        for a in report["attempts"]
        if a["attempt_id"] in {m.get("attempt_id") for m in report["main"]}
    )
    require(
        stamp(reference_plan["declared_at"]) > last_main, "recovery plan predates main evidence"
    )
    comparisons = []
    for name in ("W1", "W2"):
        entries = [s for s in reference["studies"] if s["workload"] == name]
        require(
            len(entries) == 1 and entries[0]["strategy"] == "grid_characterization",
            "missing declared reference",
        )
        entry = entries[0]
        require(
            stamp(entry["request_started_at"]) > stamp(reference_plan["declared_at"]),
            "undeclared reference execution",
        )
        require(
            stamp(entry["request_started_at"]) > last_main, "reference leaked into main execution"
        )
        spec = WorkloadSpec.model_validate(entry["study"]["spec"])
        rows = [s for s in selections if s["workload"] == name]
        require(len(rows) == 2, "missing paired search")
        for source in rows:
            original = WorkloadSpec.model_validate(source["study"]["spec"])
            require(
                spec.identity == original.identity and spec.quality == original.quality,
                "reference logical workload/quality mismatch",
            )
            require(
                {c.ref: c.context for c in spec.candidates}
                == {c.ref: c.context for c in original.candidates},
                "reference candidate context mismatch",
            )
            require(
                stamp(entry["request_started_at"]) > stamp(source["terminal_observed_at"]),
                "reference leaked into search",
            )
        # Runtime aliases may differ; actual image and entrypoint must not.
        originals = [
            a for a in report["attempts"] if a["spec"]["ref"] == rows[0]["study"]["spec"]["ref"]
        ]
        recovery = [a for a in reference["attempts"] if a["spec"]["ref"] == spec.ref]
        require(originals and recovery, "missing runtime evidence")
        for a in recovery:
            require(
                all(
                    a["variant"][k] == originals[0]["variant"][k]
                    for k in (
                        "image",
                        "command",
                        "pilot_command",
                        "environment_digest",
                        "model_digest",
                    )
                ),
                "reference runtime changed",
            )
        groups = {}
        for observation in entry["study"]["observations"]:
            if observation["mode"] == "confirmation" and observation["outcome"] == "COMPLETED":
                groups.setdefault(observation["candidate_ref"], []).append(
                    observation["measurements"]["elapsed_seconds"]
                )
        complete = (
            entry["study"]["state"] == "COMPLETED"
            and set(groups) == {c.ref for c in spec.candidates}
            and all(len(v) >= spec.quality.minimum_repeats for v in groups.values())
        )
        means = {k: sum(v) / len(v) for k, v in groups.items()} if complete else {}
        for source in rows:
            recommendation = source["study"].get("recommendation")
            selected = (
                recommendation["candidate_ref"]
                if recommendation and recommendation["measured"]
                else None
            )
            comparisons.append(
                {
                    "workload": name,
                    "strategy": source["strategy"],
                    "selected_candidate": selected,
                    "reference_complete": complete,
                    "reference_means_seconds": means,
                    "reference_best_candidate": min(means, key=means.get) if means else None,
                    "relative_distance_to_measured_reference": (
                        means[selected] - min(means.values())
                    )
                    / min(means.values())
                    if selected in means
                    else None,
                    "reference_attempt_ids": [
                        o["attempt_id"]
                        for o in entry["study"]["observations"]
                        if o["mode"] == "confirmation"
                    ],
                    "scope": "Later measured finite-space mean reference, not a true oracle, confidence bound, or causal optimizer advantage",
                }
            )
    total = dict(primary["reservation_by_unit"])
    for unit, value in later["reservation_by_unit"].items():
        total[unit] = total.get(unit, 0) + value
    return {
        "schema_version": "right-sizing-reference-comparison-v1",
        "primary_experiment_id": report["experiment_id"],
        "reference_experiment_id": reference["experiment_id"],
        "comparison": comparisons,
        "total_gpu_native_jobs": primary["attempt_count"]
        + primary["qualification_count"]
        + later["attempt_count"],
        "primary_reservation_by_unit": primary["reservation_by_unit"],
        "supplementary_reservation_by_unit": later["reservation_by_unit"],
        "combined_reservation_by_unit": total,
        "failures_including_original_reference": primary["failures"] + later["failures"],
        "cost_policy": "Primary finite-N deployment curves unchanged. Both failed v1 and supplementary v2 characterization costs retained in total research usage.",
        "overall_goal_complete": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--phase", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--reference-plan", type=Path)
    args = parser.parse_args()
    report, plan = json.loads(args.report.read_text()), json.loads(args.plan.read_text())
    result = (
        audit_hailo_feedback(report, plan)
        if report.get("kind") == "hailo-platform-integration"
        else audit(report, plan)
    )
    if bool(args.reference) != bool(args.reference_plan):
        parser.error("--reference and --reference-plan must be supplied together")
    if args.phase:
        import hashlib

        result = audit_phase_readback(
            json.loads(args.phase.read_text()),
            report,
            hashlib.sha256(args.report.read_bytes()).hexdigest(),
        )
    if args.reference:
        result = compare_reference(
            report,
            plan,
            json.loads(args.reference.read_text()),
            json.loads(args.reference_plan.read_text()),
        )
    rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered)
