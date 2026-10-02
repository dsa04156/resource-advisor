"""Read-only forecast audits and conservative recommendation reuse gates."""

import math
import statistics
from datetime import datetime

from sqlalchemy import select

from .contracts import State, now, signature
from .policy import compatibility, context_signature
from .store import jobs, studies

POLICY = "recommendation-reuse-v1"
RESIDUAL_LIMIT = 0.25
CONSECUTIVE_RUNS = 3


def recent_profile_window(matches, minimum_repeats):
    """Compare adjacent recent blocks; don't pool a detected old/new regime."""
    block = max(CONSECUTIVE_RUNS, minimum_repeats)
    recent = sorted(matches, key=lambda item: (item[2], item[0]))[-2 * block :]
    if len(recent) < 2 * block:
        return recent, []
    reference = statistics.mean(item[1]["elapsed_seconds"] for item in recent[:block])
    errors = [(item[1]["elapsed_seconds"] - reference) / reference for item in recent[block:]]
    if all(e > RESIDUAL_LIMIT for e in errors) or all(e < -RESIDUAL_LIMIT for e in errors):
        return recent, [item[0] for item in recent[block:]]
    return recent, []


def stamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def assess_recommendation(service, conn, project, rec, *, at=None):
    """No mutation, refitting, automatic resource change, or running-job cancellation."""
    at = at or now()
    reasons, residuals = [], []
    created, expires = stamp(rec.get("created_at")), stamp(rec.get("expires_at"))
    if created is None or expires is None or created > at or expires <= at:
        reasons.append("RECOMMENDATION_EXPIRED_OR_TIME_INVALID")
    if not rec.get("measured") or not rec.get("candidate_ref"):
        reasons.append("NO_MEASURED_RECOMMENDATION")
    report = {
        "policy_version": POLICY,
        "assessed_at": at.isoformat(),
        "recommendation_ref": rec["ref"],
        "reusable": False,
        "reasons": reasons,
        "residuals": residuals,
        "drift_policy": {
            "relative_error_limit": RESIDUAL_LIMIT,
            "consecutive_independent_runs": CONSECUTIVE_RUNS,
            "semantics": "heuristic recheck trigger, not calibrated probability or root cause",
        },
    }
    if not rec.get("candidate_ref"):
        return report
    # Import lazily: Service uses this module at approval/submission boundaries.
    from .service import NotFound, Rejected

    try:
        spec, candidate, variant, cap = service.bundle(
            conn, project, rec["workload_ref"], rec["candidate_ref"]
        )
    except (NotFound, Rejected):
        reasons.append("CURRENT_QUALIFICATION_UNAVAILABLE")
        return report
    reasons.extend(compatibility(spec, candidate, variant, cap, at))
    if signature(spec) != rec["workload_digest"]:
        reasons.append("WORKLOAD_SCOPE_CHANGED")
    expected_workload, expected_context = (
        signature(spec.identity),
        context_signature(candidate, variant),
    )
    rank = next((r for r in rec.get("ranking", []) if r["candidate_ref"] == candidate.ref), None)
    if not rank or len(set(rank.get("evidence_refs", []))) < spec.quality.minimum_repeats:
        reasons.append("SUPPORTING_EVIDENCE_INSUFFICIENT")
        return report
    support = set(rank["evidence_refs"])
    for ref in support:
        profile = service.store.get(conn, "profile", ref)
        if not profile or profile["project"] != project:
            reasons.append("SUPPORTING_PROFILE_MISSING")
            continue
        body, result = profile["body"], profile["body"]["result"]
        recorded = stamp(body.get("recorded_at"))
        if (
            recorded is None
            or created is None
            or recorded > created
            or not 0 <= (at - recorded).total_seconds() <= spec.quality.max_profile_age_seconds
        ):
            reasons.append("SUPPORTING_PROFILE_STALE_OR_FUTURE")
        if (
            result["workload_signature"] != expected_workload
            or result["context_signature"] != expected_context
            or result["outcome"] != "COMPLETED"
            or not result["measured"]
            or (result["evidence_kind"] != "hardware" and not service.accept_synthetic)
        ):
            reasons.append("SUPPORTING_PROFILE_SCOPE_MISMATCH")
    expected = rank["mean_seconds"]
    if not isinstance(expected, (int, float)) or not math.isfinite(expected) or expected <= 0:
        reasons.append("INVALID_REFERENCE_MEAN")
        return report
    failures = []
    query = select(jobs).where(jobs.c.project == project)
    for row in conn.execute(query).mappings():
        body = row["body"]
        started, finished = stamp(body.get("created_at")), stamp(body.get("finished_at"))
        if (
            created is None
            or started is None
            or finished is None
            or not created < started <= finished <= at
            or body["attempt_id"] in support
            or body["workload_signature"] != expected_workload
            or body["context_signature"] != expected_context
            or body["request"]["mode"] == "pilot"
        ):
            continue
        saved = service.store.get(conn, "result", body["attempt_id"])
        result = saved["body"] if saved and row["state"] != State.RESULT_INVALID else None
        if result and (result["evidence_kind"] == "hardware" or service.accept_synthetic):
            m = result.get("measurements")
            if result["outcome"] in {"OOM", "TIMEOUT"} or (
                m
                and (
                    m["quality_value"] < spec.quality.minimum
                    or m["peak_memory_mib"] > spec.quality.maximum_peak_memory_mib
                )
            ):
                failures.append(body["attempt_id"])
            elif row["state"] == State.SUCCEEDED and m:
                residuals.append(
                    {
                        "attempt_id": body["attempt_id"],
                        "recorded_at": finished.isoformat(),
                        "actual_seconds": m["elapsed_seconds"],
                        "relative_error": (m["elapsed_seconds"] - expected) / expected,
                    }
                )
        elif body.get("error") in {"OUT_OF_MEMORY", "TIMEOUT"}:
            # A scheduler-observed failure is a recheck trigger, not a performance sample.
            failures.append(body["attempt_id"])
    residuals.sort(key=lambda r: (r["recorded_at"], r["attempt_id"]))
    # Latch after any consecutive excursion: later mixed observations cannot revive
    # an old recommendation. A new independently supported recommendation is required.
    drift = next(
        (
            residuals[i : i + CONSECUTIVE_RUNS]
            for i in range(len(residuals) - CONSECUTIVE_RUNS + 1)
            if all(
                r["relative_error"] > RESIDUAL_LIMIT for r in residuals[i : i + CONSECUTIVE_RUNS]
            )
            or all(
                r["relative_error"] < -RESIDUAL_LIMIT for r in residuals[i : i + CONSECUTIVE_RUNS]
            )
        ),
        [],
    )
    if drift:
        reasons.append("CONSECUTIVE_RESIDUAL_DRIFT")
    if failures:
        reasons.append("POST_RECOMMENDATION_CONSTRAINT_FAILURE")
    report.update(
        reusable=not reasons,
        reasons=sorted(set(reasons)),
        drift_evidence_refs=[r["attempt_id"] for r in drift],
        constraint_failure_refs=failures,
    )
    return report


def shadow_report(store, project):
    """Score only forecasts durably saved before the held-out target was submitted."""
    accepted, excluded, workload_groups, seen = [], [], set(), set()
    with store.transaction() as conn:
        rows = list(conn.execute(select(jobs).where(jobs.c.project == project)).mappings())
        by_attempt = {r["body"]["attempt_id"]: r for r in rows}
        for study in conn.execute(select(studies).where(studies.c.project == project)).mappings():
            identity = signature(study["body"]["spec"]["identity"])
            workload_groups.add(identity)
            for observation in study["body"]["observations"]:
                attempt = observation["attempt_id"]
                plan_row = store.get(conn, "probe_plan", observation["plan_ref"])
                if not plan_row or plan_row["project"] != project:
                    excluded.append({"attempt_id": attempt, "reason": "PLAN_MISSING"})
                    continue
                plan = plan_row["body"]
                choice = plan["choice"]
                prediction = next(
                    (
                        p
                        for p in choice.get("predictions", [])
                        if p["candidate_ref"] == observation["candidate_ref"]
                    ),
                    None,
                )
                if prediction is None:
                    excluded.append({"attempt_id": attempt, "reason": "NO_SAVED_PREDICTION"})
                    continue
                target = by_attempt.get(attempt)
                result_row = store.get(conn, "result", attempt)
                result = (
                    result_row["body"] if result_row and result_row["project"] == project else None
                )
                source_ids = choice.get("surrogate", {}).get("training_run_ids", [])
                sources_valid = True
                for source in source_ids:
                    saved = store.get(conn, "result", source)
                    if (
                        not saved
                        or saved["project"] != project
                        or saved["body"]["evidence_kind"] != "hardware"
                        or saved["body"]["outcome"] != "COMPLETED"
                        or not saved["body"].get("measurements")
                        or source not in by_attempt
                        or by_attempt[source]["state"] != State.SUCCEEDED
                    ):
                        sources_valid = False
                cutoff = stamp(plan.get("created_at"))
                target_start = stamp(target["body"].get("created_at")) if target else None
                reason = None
                if attempt in seen:
                    reason = "DUPLICATE_TARGET"
                elif not source_ids or attempt in source_ids:
                    reason = "TRAIN_TARGET_OVERLAP_OR_MISSING_PROVENANCE"
                elif not sources_valid:
                    reason = "TRAINING_NOT_VALIDATED_HARDWARE"
                elif not cutoff or not target_start or cutoff >= target_start:
                    reason = "FORECAST_NOT_BEFORE_TARGET"
                elif any(
                    source not in by_attempt
                    or not stamp(by_attempt[source]["body"].get("finished_at"))
                    or stamp(by_attempt[source]["body"]["finished_at"]) > cutoff
                    or by_attempt[source]["body"]["workload_signature"] != identity
                    for source in source_ids
                ):
                    reason = "TRAINING_SCOPE_OR_TIME_LEAKAGE"
                elif (
                    not target
                    or target["state"] != State.SUCCEEDED
                    or not result
                    or result["evidence_kind"] != "hardware"
                    or result["outcome"] != "COMPLETED"
                    or not result["measured"]
                    or not result.get("measurements")
                    or result["workload_signature"] != identity
                    or result["context_signature"] != target["body"]["context_signature"]
                    or signature(target["body"]["spec"]) != plan["workload_digest"]
                    or plan["workload_digest"] != signature(study["body"]["spec"])
                    or plan["study_ref"] != study["id"]
                    or target["body"]["request"].get("probe_plan_ref") != plan["ref"]
                    or target["body"]["candidate"]["ref"] != prediction["candidate_ref"]
                ):
                    reason = "TARGET_NOT_COMPARABLE_HARDWARE_RESULT"
                interval = prediction.get("posterior_interval_seconds", [])
                mean = prediction.get("predicted_elapsed_seconds")
                if not reason and (
                    prediction.get("measured") is not False
                    or len(interval) != 2
                    or not all(
                        isinstance(v, (int, float)) and math.isfinite(v) and v > 0
                        for v in [mean, *interval]
                    )
                    or not interval[0] <= mean <= interval[1]
                ):
                    reason = "INVALID_FORECAST"
                if reason:
                    excluded.append({"attempt_id": attempt, "reason": reason})
                    continue
                actual = result["measurements"]["elapsed_seconds"]
                quality = study["body"]["spec"]["quality"]
                accepted.append(
                    {
                        "attempt_id": attempt,
                        "plan_ref": plan["ref"],
                        "workload_signature": identity,
                        "training_run_ids": source_ids,
                        "forecast_at": cutoff.isoformat(),
                        "target_submitted_at": target_start.isoformat(),
                        "predicted_seconds": mean,
                        "interval_seconds": interval,
                        "actual_seconds": actual,
                        "covered": interval[0] <= actual <= interval[1],
                        "interval_width_seconds": interval[1] - interval[0],
                        "absolute_relative_error": abs(actual - mean) / actual,
                        "constraint_violation": (
                            result["measurements"]["quality_value"] < quality["minimum"]
                            or result["measurements"]["peak_memory_mib"]
                            > quality["maximum_peak_memory_mib"]
                        ),
                    }
                )
                seen.add(attempt)
    groups = {}
    for row in accepted:
        groups.setdefault(row["workload_signature"], []).append(row)
    return {
        "policy_version": "shadow-chronological-v1",
        "generated_at": now().isoformat(),
        "split": "saved forecast before target submission; all training completions before forecast",
        "evaluated": accepted,
        "excluded": excluded,
        "by_workload": [
            {
                "workload_signature": key,
                "count": len(values),
                "empirical_interval_coverage": statistics.mean(float(r["covered"]) for r in values),
                "mean_interval_width_seconds": statistics.mean(
                    r["interval_width_seconds"] for r in values
                ),
                "mean_absolute_relative_error": statistics.mean(
                    r["absolute_relative_error"] for r in values
                ),
                "completed_target_constraint_violation_rate": statistics.mean(
                    float(r["constraint_violation"]) for r in values
                ),
            }
            for key, values in sorted(groups.items())
        ],
        "workload_holdout": {
            "status": "NOT_QUALIFIED",
            "distinct_workloads": len(workload_groups),
            "reason": "Current surrogate is workload-scoped; cross-workload transfer/holdout not implemented",
        },
        "semantics": "Descriptive time holdout audit; small correlated samples do not calibrate a 95% operational guarantee",
    }
