"""S5 offline reuse-gate replay using only evidence available before each target."""

import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from audit_load_drift import audit

from resource_advisor.contracts import signature
from resource_advisor.uncertainty import POLICY, RESIDUAL_LIMIT

REMOVED_REASON = "CONSECUTIVE_RESIDUAL_DRIFT"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def describe(values):
    return {
        "n": len(values),
        "raw_seconds": values,
        "mean_seconds": statistics.mean(values),
        "sample_sd_seconds": statistics.stdev(values) if len(values) > 1 else None,
        "median_seconds": statistics.median(values),
        "missing_values": 0,
    }


def evaluate(report):
    audited = audit(report)
    runs, rec = report["runs"], report["recommendation"]
    support = runs[:3]
    require(
        rec["independent_runs"] == 3
        and set(rec["evidence_refs"]) == {r["result"]["attempt_id"] for r in support},
        "baseline support changed",
    )
    baseline = audited["baseline_mean_seconds"]
    radius = (
        3
        * statistics.stdev(r["result"]["measurements"]["elapsed_seconds"] for r in support)
        / math.sqrt(3)
    )
    interval = [max(0, baseline - radius), baseline + radius]
    require(
        len(rec["interval_seconds"]) == 2
        and all(
            math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-12)
            for a, b in zip(interval, rec["interval_seconds"], strict=True)
        ),
        "original forecast interval changed",
    )
    created = datetime.fromisoformat(rec["created_at"])
    decisions = []
    for index in range(3, 9):
        target = runs[index]
        started = datetime.fromisoformat(target["started_at"])
        require(created < started, "recommendation was not before target")
        previous = runs[index - 1] if index > 3 else None
        available = runs[3:index]
        require(
            all(datetime.fromisoformat(r["verified_at"]) < started for r in available),
            "unverified or future follow-up used for decision",
        )
        reasons = []
        assessment_at = rec["created_at"]
        if previous:
            validity = previous["recommendation_validity"]
            require(
                validity["policy_version"] == POLICY
                and validity["recommendation_ref"] == rec["ref"],
                "foreign recommendation or policy",
            )
            assessment_at = validity["assessed_at"]
            require(
                datetime.fromisoformat(previous["verified_at"])
                <= datetime.fromisoformat(assessment_at)
                < started,
                "assessment unavailable before target submission",
            )
            residuals = validity["residuals"]
            require(
                [r["attempt_id"] for r in residuals]
                == [r["result"]["attempt_id"] for r in available],
                "residual cohort includes missing, foreign or current/future targets",
            )
            for residual, record in zip(residuals, available, strict=True):
                elapsed = record["result"]["measurements"]["elapsed_seconds"]
                require(
                    math.isclose(residual["actual_seconds"], elapsed, rel_tol=1e-12)
                    and math.isclose(
                        residual["relative_error"], (elapsed - baseline) / baseline, rel_tol=1e-12
                    )
                    and datetime.fromisoformat(residual["recorded_at"]) < started,
                    "residual value/time differs from prior evidence",
                )
            reasons = sorted(set(validity["reasons"]))
            require(validity["reusable"] == (not reasons), "recorded reuse/reasons disagree")
        # Outcomes below score the frozen decisions; they never enter their masks.
        actual = target["result"]["measurements"]["elapsed_seconds"]
        ablated_reasons = [reason for reason in reasons if reason != REMOVED_REASON]
        decisions.append(
            {
                "target_index": index,
                "target_attempt_id": target["result"]["attempt_id"],
                "condition": target["condition"],
                "target_started_at": target["started_at"],
                "assessment_available_at": assessment_at,
                "available_followup_attempt_ids": [r["result"]["attempt_id"] for r in available],
                "strict_reasons": reasons,
                "without_drift_reasons": ablated_reasons,
                "strict_reuse": not reasons,
                "without_drift_reuse": not ablated_reasons,
                "actual_seconds": actual,
                "predicted_seconds": baseline,
                "interval_seconds": interval,
                "covered": interval[0] <= actual <= interval[1],
                "relative_residual": (actual - baseline) / baseline,
                "absolute_relative_error": abs(actual - baseline) / actual,
                "forecast_tolerance_exceeded": abs(actual - baseline) / baseline > RESIDUAL_LIMIT,
                "quality_value": target["result"]["measurements"]["quality_value"],
                "peak_memory_mib": target["result"]["measurements"]["peak_memory_mib"],
            }
        )
    rates = {}
    for policy in ("strict", "without_drift"):
        accepted = [d for d in decisions if d[policy + "_reuse"]]
        rates[policy] = {
            "denominator_followups": len(decisions),
            "reuse_count": len(accepted),
            "abstention_count": len(decisions) - len(accepted),
            "reuse_fraction": len(accepted) / len(decisions),
            "forecast_tolerance_exceedance_count": sum(
                d["forecast_tolerance_exceeded"] for d in accepted
            ),
            "heuristic_interval_coverage": statistics.mean(d["covered"] for d in accepted)
            if accepted
            else None,
            "mean_absolute_relative_error": statistics.mean(
                d["absolute_relative_error"] for d in accepted
            )
            if accepted
            else None,
            "accepted_subset_denominator": len(accepted),
        }
    return {
        "schema_version": "offline-uncertainty-drift-ablation-v1",
        "source_capture_digest": signature(report),
        "scope": "Exploratory chronological replay of one recorded reuse reason; no new workload or search trajectory",
        "only_removed_reason": REMOVED_REASON,
        "baseline_support_attempt_ids": rec["evidence_refs"],
        "decisions": decisions,
        "rates": rates,
        "changed_decisions": sum(d["strict_reuse"] != d["without_drift_reuse"] for d in decisions),
        "all_followup_evaluation": {
            "n": len(decisions),
            "heuristic_interval_coverage": statistics.mean(d["covered"] for d in decisions),
            "mean_absolute_relative_error": statistics.mean(
                d["absolute_relative_error"] for d in decisions
            ),
            "forecast_tolerance_exceedance_count": sum(
                d["forecast_tolerance_exceeded"] for d in decisions
            ),
            "quality_memory_violations": sum(
                d["quality_value"] < 1 or d["peak_memory_mib"] > 2048 for d in decisions
            ),
        },
        "phase_descriptives": {
            label: describe([r["result"]["measurements"]["elapsed_seconds"] for r in phase])
            for label, phase in [
                ("support", runs[:3]),
                ("competitor", runs[3:6]),
                ("recovery", runs[6:]),
            ]
        },
        "new_gpu_jobs": 0,
        "original_gpu_reservation_seconds_not_saved": audited["gpu_reservation_seconds"],
        "quality_memory_and_other_reasons_preserved": True,
        "limitations": [
            "One fixed-order workload/device sequence is confounded with time; no significance test or causal superiority estimate.",
            "The target's result is evaluation-only. Its drift assessment can affect only subsequent targets.",
            "A forecast-tolerance exceedance is not proof of a wrong resource configuration; no alternative candidate or resource regret is evaluated.",
            "Accepted-subset error rates have different denominators and are not a paired effectiveness estimate.",
            "Hypothetical reuse/abstention masks are not extra deployments, prevented failures or measured resource savings.",
            "The original mean +/- 3 standard-error interval is not calibrated future-Job coverage.",
        ],
    }


def evaluate_capture(capture, plan):
    require(plan["schema_version"] == "s5-drift-ablation-plan-v1", "wrong frozen plan")
    require(
        hashlib.sha256(capture.read_bytes()).hexdigest() == plan["source_file_sha256"],
        "capture bytes changed",
    )
    report = json.loads(capture.read_text())
    require(signature(report) == plan["source_capture_digest"], "capture content changed")
    require(
        plan["target_indices"] == list(range(3, 9))
        and plan["support_indices"] == [0, 1, 2]
        and plan["only_removed_reason"] == REMOVED_REASON
        and plan["relative_error_limit"] == RESIDUAL_LIMIT
        and plan["new_gpu_jobs"] == 0,
        "replay scope changed",
    )
    return evaluate(report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    output = evaluate_capture(args.capture, plan)
    with args.output.open("x") as handle:
        json.dump(output, handle, indent=2, allow_nan=False)
        handle.write("\n")
