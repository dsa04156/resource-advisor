"""Audit a captured transfer protocol and report descriptive costs and selection regret.

No API access, model fitting, p-values or improvement claims. Oracle measurements
are used only after all tuning studies have completed.
"""

import argparse
import json
import math
import statistics
from datetime import datetime
from pathlib import Path


def summarize(report):
    studies, plans, design = report["studies"], report["plans"], report["plan"]
    rows = report["observations"]
    evidence = report["source_evidence"]
    source_ids = {p["attempt_id"] for p in evidence["provenance"]}
    observed_ids = [o["attempt_id"] for study in studies.values() for o in study["observations"]]
    if len(observed_ids) != len(set(observed_ids)):
        raise ValueError("independent study attempts overlap")
    if len(rows) != len(observed_ids) or {r["attempt_id"] for r in rows} != set(observed_ids):
        raise ValueError("captured accounting rows do not cover all attempts exactly once")
    for row in rows:
        value = row["allocated_device_seconds"]
        if value is None or not math.isfinite(value) or value < 0:
            raise ValueError("unknown or invalid GPU allocation cost cannot be counted as zero")
        queue = row["queue_seconds"]
        if queue is not None and (not math.isfinite(queue) or queue < 0):
            raise ValueError("invalid queue duration")
    by_attempt = {r["attempt_id"]: r for r in rows}
    for study in studies.values():
        for obs in study["observations"]:
            if by_attempt[obs["attempt_id"]]["study_ref"] != study["ref"]:
                raise ValueError("accounting attempt assigned to the wrong study")
    source_confirmations = {
        o["attempt_id"]
        for label, s in studies.items()
        if label.startswith("source-")
        for o in s["observations"]
        if o["mode"] == "confirmation"
    }
    if source_ids != source_confirmations:
        raise ValueError("source snapshot must retain the entire preregistered confirmation cohort")
    target_labels = [f"target-{s['block']}-{s['strategy']}" for s in design["target_schedule"]]
    if not target_labels or len(set(target_labels)) != len(target_labels):
        raise ValueError("invalid preregistered target schedule")
    if any(label not in studies for label in target_labels):
        raise ValueError("missing preregistered target study")
    target_studies = [studies[label] for label in target_labels]
    target_spec = target_studies[0]["spec"]
    if any(s["spec"] != target_spec for s in target_studies):
        raise ValueError("comparison workload or approved budgets differ")
    approved = design["per_target_study"]
    policy = target_spec["profiling"]
    if any(
        policy[k] != approved[k]
        for k in ("max_probes", "total_wall_seconds", "final_validation_seconds")
    ) or (
        len(policy["device_seconds"]) != 1
        or next(iter(policy["device_seconds"].values())) != approved["device_seconds"]
        or target_spec["quality"]["minimum_repeats"] != approved["minimum_confirmation_repeats"]
    ):
        raise ValueError("study budgets differ from the frozen protocol")
    for slot, study in zip(design["target_schedule"], target_studies, strict=True):
        if any(study["request"][k] != slot[k] for k in ("strategy", "seed")):
            raise ValueError("target strategy or seed differs from the frozen protocol")
    if any(
        datetime.fromisoformat(left["created_at"]) >= datetime.fromisoformat(right["created_at"])
        for left, right in zip(target_studies, target_studies[1:], strict=False)
    ):
        raise ValueError("target study order differs from the frozen protocol")
    if any(
        s["state"] not in {"COMPLETED", "ABSTAINED", "FAILED", "CANCELED"} for s in studies.values()
    ):
        raise ValueError("protocol contains a live study")
    oracle = studies["oracle"]
    oracle_start = datetime.fromisoformat(oracle["created_at"])
    if oracle["state"] != "COMPLETED" or oracle["spec"] != target_spec:
        raise ValueError("post-hoc oracle did not complete")
    for s in target_studies:
        if datetime.fromisoformat(s["created_at"]) <= datetime.fromisoformat(
            evidence["recorded_at"]
        ):
            raise ValueError("source cohort was not frozen before target work")
        if any(datetime.fromisoformat(o["recorded_at"]) >= oracle_start for o in s["observations"]):
            raise ValueError("oracle measurements overlap target tuning")
    by_ref = {s["ref"]: s for s in studies.values()}
    plan_ids = [p["ref"] for p in plans]
    expected_plans = [p for s in studies.values() for p in s["plans"]]
    if len(plan_ids) != len(set(plan_ids)) or set(plan_ids) != set(expected_plans):
        raise ValueError("captured plans do not cover the protocol exactly once")
    audits = []
    for plan in plans:
        choice = plan["choice"]
        s = by_ref[plan["study_ref"]]
        if plan["ref"] not in s["plans"]:
            raise ValueError("plan assigned to the wrong study")
        allowed = {
            o["attempt_id"]
            for o in s["observations"]
            if o["mode"] == "pilot"
            and datetime.fromisoformat(o["recorded_at"])
            < datetime.fromisoformat(plan["created_at"])
        }
        training = (
            set(choice.get("surrogate", {}).get("training_run_ids", []))
            | set(choice.get("target_run_ids", []))
            | set(choice.get("warm_start", {}).get("target_run_ids", []))
        )
        transferred = (
            set(choice.get("surrogate", {}).get("source_run_ids", []))
            | set(choice.get("source_run_ids", []))
            | set(choice.get("warm_start", {}).get("source_run_ids", []))
        )
        if not training <= allowed or (transferred and transferred != source_ids):
            raise ValueError(
                "surrogate contains future, confirmation or unauthorized source evidence"
            )
        if training & source_ids:
            raise ValueError("source attempts were relabeled as target observations")
        if choice.get("method") == "rank_weighted_gp_ensemble_qLogNEI":
            if transferred != source_ids or not training:
                raise ValueError("RGPE fit lacks its separate source/target evidence")
            audits.append(
                {
                    "plan_ref": plan["ref"],
                    "study_ref": s["ref"],
                    "source_count": len(transferred),
                    "target_count": len(training),
                    "weights": choice["weights"],
                    "rank_diagnostics": choice["rank_diagnostics"],
                    "fallback_reason": choice.get("fallback_reason"),
                }
            )
    oracle_runs = [o for o in oracle["observations"] if o["mode"] == "confirmation"]
    candidate_refs = {c["ref"] for c in target_spec["candidates"]}
    minimum_repeats = target_spec["quality"]["minimum_repeats"]
    means = {}
    for candidate in sorted(candidate_refs):
        runs = [o for o in oracle_runs if o["candidate_ref"] == candidate]
        if any(
            o["outcome"] != "COMPLETED"
            or o["measurements"]["quality_value"] < target_spec["quality"]["minimum"]
            or o["measurements"]["peak_memory_mib"]
            > target_spec["quality"]["maximum_peak_memory_mib"]
            or not math.isfinite(o["measurements"]["elapsed_seconds"])
            or o["measurements"]["elapsed_seconds"] <= 0
            for o in runs
        ):
            raise ValueError("oracle confirmation is not feasible")
        values = [o["measurements"]["elapsed_seconds"] for o in runs]
        if len(values) < minimum_repeats:
            raise ValueError("oracle lacks independent confirmation for a candidate")
        means[candidate] = statistics.mean(values)
    best = min(means.values())
    summaries = []
    for label in target_labels:
        s = studies[label]
        records = [r for r in rows if r["study_ref"] == s["ref"]]
        if any(r["allocated_device_seconds"] is None for r in records):
            raise ValueError("unknown GPU allocation cost cannot be counted as zero")
        rec = s.get("recommendation")
        selected = rec["confirmed_candidate"] if rec else None
        own_mean = (
            next(
                (r["mean_seconds"] for r in rec["ranking"] if r["candidate_ref"] == selected), None
            )
            if rec
            else None
        )
        summaries.append(
            {
                "label": label,
                "strategy": s["request"]["strategy"],
                "seed": s["request"]["seed"],
                "state": s["state"],
                "recommendation_status": rec["status"] if rec else None,
                "selected_candidate": selected,
                "independent_confirmation_mean_seconds": own_mean,
                "posthoc_selection_regret_fraction": means[selected] / best - 1
                if selected
                else None,
                "allocated_gpu_seconds": sum(r["allocated_device_seconds"] for r in records),
                "queue_seconds": sum(
                    r["queue_seconds"] for r in records if r["queue_seconds"] is not None
                ),
                "unknown_queue_intervals": sum(r["queue_seconds"] is None for r in records),
                "study_wall_seconds": rec["cost"]["wall_seconds"] if rec else None,
                "planning_seconds": s["planning_seconds"],
                "pilot_jobs": sum(o["mode"] == "pilot" for o in s["observations"]),
                "confirmation_jobs": sum(o["mode"] == "confirmation" for o in s["observations"]),
                "failed_or_incomplete_jobs": sum(
                    o["outcome"] != "COMPLETED" for o in s["observations"]
                ),
                "stop_reason": s.get("stop_reason"),
            }
        )
    source_studies = [s for label, s in studies.items() if label.startswith("source-")]
    if len(source_studies) != len(design["sources"]) or any(
        s["state"] != "COMPLETED" for s in source_studies
    ):
        raise ValueError("source characterization cohort is incomplete")
    source_refs = {s["ref"] for s in source_studies}
    source_records = [r for r in rows if r["study_ref"] in source_refs]
    if any(r["allocated_device_seconds"] is None for r in source_records):
        raise ValueError("unknown source cost cannot be counted as zero")
    return {
        "scope": "descriptive bounded GPU comparison; no powered superiority conclusion",
        "target_studies": summaries,
        "oracle_confirmation_mean_seconds": means,
        "source_characterization_cost": {
            "all_jobs": len(source_records),
            "gpu_reservation_seconds": sum(r["allocated_device_seconds"] for r in source_records),
            "study_wall_seconds": sum(
                s["recommendation"]["cost"]["wall_seconds"] for s in source_studies
            ),
            "selected_profile_job_wall_seconds": evidence["historical_source_wall_seconds"],
            "selected_profile_jobs": len(source_ids),
        },
        "oracle_gpu_reservation_seconds": sum(
            r["allocated_device_seconds"] for r in rows if r["study_ref"] == oracle["ref"]
        ),
        "rgpe_updates": audits,
        "source_target_confirmation_and_oracle_leakage_audit_passed": True,
        "same_target_workload_and_approved_budgets": True,
        "limitations": [
            "Two study blocks provide functional replication, not a powered method comparison.",
            "Post-hoc regret describes configuration selection against later measurements; time drift remains possible.",
            "Block time includes 32 forwards; it is not individual request latency.",
            "Source costs are separate from incremental target costs; qualify any amortization assumption.",
            "GPU reservation is not utilization or energy; preparation and external setup remain separate.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = summarize(json.loads(args.input.read_text()))
    with args.output.open("x") as handle:
        handle.write(json.dumps(report, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
