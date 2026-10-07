"""Evidence projections and immutable feedback; no scheduler or execution authority."""

import math

from sqlalchemy import select

from .contracts import TERMINAL, State, WorkloadSpec, now, signature
from .policy import compatibility, context_signature
from .service import NotFound, Rejected, required
from .store import jobs, studies
from .uncertainty import assess_recommendation, stamp

STAGES = (
    "UNKNOWN",
    "COMPATIBILITY_CHECKED",
    "NEEDS_PROFILE",
    "PROFILING",
    "PROFILED",
    "RECOMMENDABLE",
    "RECOMMENDED",
    "APPROVED",
    "EXECUTED",
    "VERIFIED",
)


def record_feedback(store, conn, row, state, body):
    """Called in the terminal transaction; failed/invalid results stay censored."""
    approval_ref = body.get("request", {}).get("approval_ref")
    if not approval_ref:
        return
    project, attempt = row["project"], body["attempt_id"]
    approval = store.get(conn, "approval", approval_ref)
    rec = (
        store.get(conn, "recommendation", approval["body"]["recommendation_ref"])
        if approval
        else None
    )
    result = store.get(conn, "result", attempt)
    result_body = result["body"] if result and result["project"] == project else None
    reasons = []
    rank = None
    if not approval or not rec or approval["project"] != project or rec["project"] != project:
        reasons.append("RECOMMENDATION_PROVENANCE_MISSING")
    else:
        recommendation = rec["body"]
        rank = next(
            (
                r
                for r in recommendation.get("ranking", [])
                if r["candidate_ref"] == body["candidate"]["ref"]
            ),
            None,
        )
        if approval["body"]["recommendation_digest"] != rec["digest"] or recommendation[
            "workload_digest"
        ] != signature(body["spec"]):
            reasons.append("RECOMMENDATION_SCOPE_MISMATCH")
        created, submitted = stamp(recommendation.get("created_at")), stamp(body.get("created_at"))
        if not created or not submitted or created >= submitted:
            reasons.append("RECOMMENDATION_NOT_BEFORE_EXECUTION")
    source_refs = rank.get("evidence_refs", []) if rank else []
    if not source_refs or len(source_refs) != len(set(source_refs)) or attempt in source_refs:
        reasons.append("INDEPENDENT_SOURCE_EVIDENCE_MISSING")
    expected = rank.get("mean_seconds") if rank else None
    if not isinstance(expected, (float, int)) or not math.isfinite(expected) or expected <= 0:
        reasons.append("INVALID_REFERENCE_MEAN")
    if len(source_refs) < body["spec"]["quality"]["minimum_repeats"]:
        reasons.append("INSUFFICIENT_REPEAT_COUNT")
    for ref in source_refs:
        profile = store.get(conn, "profile", ref)
        if not profile or profile["project"] != project:
            reasons.append("SOURCE_PROFILE_MISSING")
            continue
        source = profile["body"]["result"]
        recorded = stamp(profile["body"].get("recorded_at"))
        cutoff = stamp(rec["body"].get("created_at")) if rec else None
        if (
            source["workload_signature"] != body["workload_signature"]
            or source["context_signature"] != body["context_signature"]
            or not recorded
            or not cutoff
            or recorded > cutoff
        ):
            reasons.append("SOURCE_SCOPE_OR_TIME_MISMATCH")
    measurements = result_body.get("measurements") if result_body else None
    if state != State.SUCCEEDED or not measurements:
        reasons.append("NO_VALID_COMPLETED_MEASUREMENT")
    elif (
        result_body["workload_signature"] != body["workload_signature"]
        or result_body["context_signature"] != body["context_signature"]
    ):
        reasons.append("ACTUAL_SCOPE_MISMATCH")
    quality = body["spec"]["quality"]
    constraint_passed = bool(
        measurements
        and body.get("quality_passed")
        and measurements["quality_value"] >= quality["minimum"]
        and measurements["peak_memory_mib"] <= quality["maximum_peak_memory_mib"]
    )
    if measurements and not constraint_passed:
        reasons.append("ACTUAL_CONSTRAINT_VIOLATION")
    comparable = not reasons and rank is not None
    actual = measurements["elapsed_seconds"] if comparable else None
    receipt = {
        "schema_version": "recommendation-feedback-v1",
        "job_id": row["id"],
        "attempt_id": attempt,
        "epoch": row["epoch"],
        "approval_ref": approval_ref,
        "recommendation_ref": rec["ref"] if rec else None,
        "recommendation_digest": rec["digest"] if rec else None,
        "source_profile_refs": source_refs,
        "workload_signature": body["workload_signature"],
        "context_signature": body["context_signature"],
        "backend": body["candidate"]["backend"],
        "native_job_id": body.get("external_id"),
        "result_digest": result["digest"] if result else None,
        "usage_attempt_id": attempt,
        "evidence_kind": result_body["evidence_kind"] if result_body else "unavailable",
        "terminal_state": str(state),
        "recorded_at": body["finished_at"],
        "status": "COMPARABLE" if comparable else "INSUFFICIENT_EVIDENCE",
        "reasons": sorted(set(reasons)),
        "constraint_passed": constraint_passed,
        "reference_mean_seconds": expected,
        "actual_measurements": measurements,
        "actual_seconds": actual,
        "residual_seconds": actual - expected if comparable else None,
        "relative_residual": (actual - expected) / expected if comparable else None,
        "reference_interval_seconds": rank.get("interval_seconds") if rank else None,
        "semantics": "Comparison with measured reference mean, not a calibrated forecast or speedup proof",
    }
    store.put(conn, "recommendation_feedback", attempt, project, receipt)


def lifecycle(service, project, workload_ref):
    """Read current evidence without creating recommendations, profiles or Jobs."""
    preview = service.recommend(project, workload_ref, persist=False)
    with service.store.transaction() as conn:
        spec = WorkloadSpec.model_validate(
            required(service.store, conn, "workload", workload_ref, project)
        )
        identity = signature(spec.identity)
        candidates, compatible = [], set()
        for candidate in spec.candidates:
            try:
                _, _, variant, cap = service.bundle(conn, project, spec.ref, candidate.ref)
                errors = compatibility(spec, candidate, variant, cap)
                context = context_signature(candidate, variant)
            except (NotFound, Rejected):
                errors, context = ["QUALIFICATION_UNAVAILABLE"], None
            if not errors:
                compatible.add(candidate.ref)
            candidates.append(
                {
                    "candidate_ref": candidate.ref,
                    "context_signature": context,
                    "compatible": not errors,
                    "reasons": errors,
                }
            )
        rows = [
            r
            for r in conn.execute(select(jobs).where(jobs.c.project == project)).mappings()
            if r["body"]["workload_signature"] == identity and r["body"]["spec"]["ref"] == spec.ref
        ]
        profile_refs = [
            p["ref"]
            for p in service.lookup_profiles(conn, project, spec)
            if p["body"]["result"]["evidence_kind"] == "hardware" or service.accept_synthetic
        ]
        workload_studies = [
            r
            for r in conn.execute(select(studies).where(studies.c.project == project)).mappings()
            if r["body"]["spec"]["ref"] == spec.ref
            and signature(r["body"]["spec"]["identity"]) == identity
        ]
        recs = [
            r
            for r in service.store.list(conn, "recommendation", project)
            if r["body"]["workload_ref"] == spec.ref
            and r["body"]["workload_digest"] == signature(spec)
        ]
        rec = max(recs, key=lambda r: (r["body"]["created_at"], r["ref"]), default=None)
        validity = assess_recommendation(service, conn, project, rec["body"]) if rec else None
        approvals = [
            r
            for r in service.store.list(conn, "approval", project)
            if rec and r["body"]["recommendation_ref"] == rec["ref"]
        ]
        approved_refs = {r["ref"] for r in approvals}
        approved_jobs = [
            r for r in rows if r["body"]["request"].get("approval_ref") in approved_refs
        ]
        feedback = [
            r
            for r in service.store.list(conn, "recommendation_feedback", project)
            if r["body"]["job_id"] in {j["id"] for j in approved_jobs}
        ]
        measured = bool(rec and rec["body"].get("measured"))
        state, reasons = "NEEDS_PROFILE", ["NO_COMPARABLE_RECOMMENDATION"]
        if profile_refs:
            state = "PROFILED"
        if preview["measured"]:
            state, reasons = "RECOMMENDABLE", []
        if measured:
            state, reasons = "RECOMMENDED", []
        if approvals:
            state = "APPROVED"
        if any(r["state"] in TERMINAL for r in approved_jobs):
            state = "EXECUTED"
        if any(
            r["body"]["status"] == "COMPARABLE"
            and r["body"]["constraint_passed"]
            and (r["body"]["evidence_kind"] == "hardware" or service.accept_synthetic)
            for r in feedback
        ):
            state = "VERIFIED"
        if any(r["state"] in {"EXPLORING", "CONFIRMING"} for r in workload_studies):
            state, reasons = "PROFILING", []
        if rec and not measured:
            state, reasons = (
                rec["body"]["status"],
                sorted({e for errors in rec["body"]["excluded"].values() for e in errors}),
            )
        if measured and not validity["reusable"]:
            state, reasons = "NEEDS_RECONFIRMATION", validity["reasons"]
        if not compatible:
            state, reasons = (
                "NO_COMPATIBLE_VARIANT",
                sorted({e for c in candidates for e in c["reasons"]}),
            )
        completed = {
            "UNKNOWN": [spec.ref],
            "COMPATIBILITY_CHECKED": [c["candidate_ref"] for c in candidates],
        }
        if profile_refs:
            completed["PROFILED"] = profile_refs
        if workload_studies:
            completed["PROFILING"] = [r["id"] for r in workload_studies]
        if preview["measured"]:
            completed["RECOMMENDABLE"] = preview["ranking"][0]["evidence_refs"]
        if measured:
            completed["RECOMMENDABLE"] = completed["RECOMMENDED"] = [rec["ref"]]
        if approvals:
            completed["APPROVED"] = sorted(approved_refs)
        completed["EXECUTED"] = [r["id"] for r in approved_jobs if r["state"] in TERMINAL]
        completed["VERIFIED"] = [
            r["ref"]
            for r in feedback
            if r["body"]["status"] == "COMPARABLE"
            and r["body"]["constraint_passed"]
            and (r["body"]["evidence_kind"] == "hardware" or service.accept_synthetic)
        ]
        return {
            "schema_version": "right-sizing-lifecycle-v1",
            "workload_ref": spec.ref,
            "workload_signature": identity,
            "workload_digest": signature(spec),
            "state": state,
            "recommendability": {
                "status": preview["status"],
                "ranking": preview["ranking"],
                "excluded": preview["excluded"],
                "approval_authorized": False,
            },
            "distribution": {
                "status": "OBSERVED_QUALIFIED_SCOPE"
                if preview["measured"]
                else "UNSEEN_OR_INSUFFICIENT_EVIDENCE"
                if compatible
                else "OUTSIDE_QUALIFIED_SCOPE",
                "in_distribution": True if preview["measured"] else None if compatible else False,
                "predictive_interval_calibration": "NOT_QUALIFIED",
                "semantics": "Exact workload/context evidence scope, not statistical OOD probability",
            },
            "reasons": reasons,
            "assessed_at": now().isoformat(),
            "candidates": candidates,
            "profile_refs": profile_refs,
            "study_refs": [r["id"] for r in workload_studies],
            "recommendation_ref": rec["ref"] if rec else None,
            "current_validity": validity,
            "feedback": [r["body"] for r in feedback],
            "additional_profiling_required": state
            in {
                "NEEDS_PROFILE",
                "PROFILED",
                "NEEDS_RECONFIRMATION",
                "INSUFFICIENT_BASELINE_EVIDENCE",
            },
            "milestones": [
                {
                    "state": stage,
                    "evidence_refs": completed.get(stage, []),
                    "observed": bool(completed.get(stage)),
                }
                for stage in STAGES
            ],
            "semantics": "Evidence projection, not a new Job state machine. VERIFIED means independent actual/reference comparison; no speedup or calibrated coverage guarantee.",
        }
