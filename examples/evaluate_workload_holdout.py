"""Retrospective workload rank holdout and chronological saved-forecast audit.

No fitting, scheduler access, new Jobs or production recommendation authorization.
The source-only rank prior and target-adapted forecasts are separate estimands.
"""

import argparse
import json
import math
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from evaluate_transfer import summarize

from resource_advisor.contracts import signature
from resource_advisor.rgpe import RGPEInput, TransferObservation, TransferTask, history_order


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "timezone-aware evidence required")
    return parsed


def finite(value, *, positive=False):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0)
    )


def validate_rows(report):
    """Bind the published result bytes/identity/timing to each study observation."""
    rows = {r["attempt_id"]: r for r in report["observations"]}
    require(len(rows) == len(report["observations"]), "duplicate captured attempt")
    for key in ("job_id", "plan_ref"):
        require(len({r[key] for r in rows.values()}) == len(rows), f"duplicate {key}")
    plans = {p["ref"]: p for p in report["plans"]}
    for study in report["studies"].values():
        for obs in study["observations"]:
            row = rows[obs["attempt_id"]]
            result, plan = row["result"], plans[obs["plan_ref"]]
            require(all(row[k] == v for k, v in obs.items()), "observation/result disagreement")
            require(
                result["attempt_id"] == obs["attempt_id"]
                and result["job_id"] == obs["job_id"]
                and result["measurements"] == obs["measurements"]
                and result["outcome"] == obs["outcome"]
                and result["evidence_kind"] == obs["evidence_kind"] == "hardware"
                and result["measured"] is True
                and signature(result) == row["result_digest"]
                and result["workload_signature"] == signature(study["spec"]["identity"]),
                "captured hardware result digest or identity mismatch",
            )
            require(
                plan["study_ref"] == row["study_ref"] == study["ref"]
                and plan["workload_ref"] == row["workload_ref"] == study["request"]["workload_ref"]
                and plan["workload_digest"] == signature(study["spec"])
                and plan["candidate_ref"] == obs["candidate_ref"]
                and plan["mode"] == obs["mode"],
                "plan scope mismatch",
            )
            times = [
                timestamp(v)
                for v in (
                    study["created_at"],
                    plan["created_at"],
                    row["submitted_at"],
                    row["started_at"],
                    row["finished_at"],
                    row["recorded_at"],
                )
            ]
            require(times == sorted(times) and times[1] < times[2], "invalid execution chronology")
            m = result["measurements"]
            require(
                m is not None
                and finite(m["elapsed_seconds"], positive=True)
                and finite(m["peak_memory_mib"])
                and type(m["quality_value"]) in (int, float)
                and math.isfinite(m["quality_value"])
                and m["work_units"] == study["spec"]["identity"]["work_units"],
                "invalid completed measurement",
            )
    return rows


def feasible(row, quality):
    m = row["measurements"]
    return (
        row["outcome"] == "COMPLETED"
        and m["quality_value"] >= quality["minimum"]
        and m["peak_memory_mib"] <= quality["maximum_peak_memory_mib"]
    )


def rank_fold(cohorts, held_out, *, runtime_group, options, quality):
    """Only source tasks reach the existing rank-prior implementation."""
    target = cohorts[held_out]
    sources = [c for label, c in cohorts.items() if label != held_out]
    source_rows = [r for c in sources for r in c["rows"]]
    require(all(feasible(r, quality) for r in source_rows), "infeasible source evidence")
    require(
        not ({r["attempt_id"] for r in source_rows} & {r["attempt_id"] for r in target["rows"]}),
        "source and held-out attempts overlap",
    )
    tasks = [
        TransferTask(
            workload_signature=c["signature"],
            runtime_group_signature=runtime_group,
            observations=[
                TransferObservation(
                    attempt_id=r["attempt_id"],
                    candidate_ref=r["candidate_ref"],
                    elapsed_seconds=r["measurements"]["elapsed_seconds"],
                    evaluation_wall_seconds=(
                        timestamp(r["finished_at"]) - timestamp(r["submitted_at"])
                    ).total_seconds(),
                    peak_memory_mib=r["measurements"]["peak_memory_mib"],
                    quality_value=r["measurements"]["quality_value"],
                    quality_passed=True,
                    memory_passed=True,
                )
                for r in c["rows"]
            ],
        )
        for c in sources
    ]
    prior = history_order(
        RGPEInput(
            runtime_group_signature=runtime_group,
            options=options,
            feature_names=["host_cpu"],
            sources=tasks,
            target=TransferTask(
                workload_signature=target["signature"],
                runtime_group_signature=runtime_group,
                observations=[],
            ),
            minimum_quality=quality["minimum"],
            maximum_peak_memory_mib=quality["maximum_peak_memory_mib"],
            rank_tie_fraction=0.05,
        )
    )
    # Held-out timings are first accessed here, after the complete ordering exists.
    selected = prior["candidate_order"][0]
    means, samples = {}, {}
    for option in options:
        ref = option["candidate_ref"]
        runs = [r for r in target["rows"] if r["candidate_ref"] == ref]
        values = [r["measurements"]["elapsed_seconds"] for r in runs]
        samples[ref] = {
            "attempt_ids": [r["attempt_id"] for r in runs],
            "raw_seconds": values,
            "mean_seconds": statistics.mean(values),
            "sample_sd_seconds": statistics.stdev(values),
            "median_seconds": statistics.median(values),
        }
        if all(feasible(r, quality) for r in runs):
            means[ref] = statistics.mean(values)
    latest_source = max(timestamp(r["recorded_at"]) for r in source_rows)
    earliest_target = min(timestamp(r["submitted_at"]) for r in target["rows"])
    return {
        "held_out": held_out,
        "workload_signature": target["signature"],
        "input_shape": target["shape"],
        "source_workload_signatures": [c["signature"] for c in sources],
        "prior": prior,
        "selected_candidate": selected,
        "held_out_samples": samples,
        "held_out_attempt_ids": [r["attempt_id"] for r in target["rows"]],
        "finite_sample_regret_fraction": means[selected] / min(means.values()) - 1
        if selected in means
        else None,
        "selected_constraint_violations": sum(
            not feasible(r, quality) for r in target["rows"] if r["candidate_ref"] == selected
        ),
        "latest_source_available_at": latest_source.isoformat(),
        "earliest_target_backend_submitted_at": earliest_target.isoformat(),
        "source_precedes_target": latest_source < earliest_target,
        "time_prediction": None,
        "interval_coverage": None,
        "interval_reason": "Rank prior produces an ordering, not target seconds or intervals",
        "execution_authorized": False,
    }


def time_forecasts(report, rows):
    accepted, excluded = [], []
    studies = {s["ref"]: s for s in report["studies"].values()}
    plans = {p["ref"]: p for p in report["plans"]}
    allowed_sources = {p["attempt_id"] for p in report["source_evidence"]["provenance"]}
    for row in rows.values():
        plan = plans[row["plan_ref"]]
        choice, study = plan["choice"], studies[row["study_ref"]]
        forecasts = [
            p for p in choice.get("predictions", []) if p["candidate_ref"] == row["candidate_ref"]
        ]
        if not forecasts:
            excluded.append({"attempt_id": row["attempt_id"], "reason": "NO_SAVED_PREDICTION"})
            continue
        require(len(forecasts) == 1, "duplicate candidate forecast")
        surrogate = choice.get("surrogate", {})
        train, sources = surrogate.get("training_run_ids", []), surrogate.get("source_run_ids", [])
        all_ids = train + sources
        require(train and len(all_ids) == len(set(all_ids)), "missing or duplicate training ids")
        require(row["attempt_id"] not in all_ids, "target leaked into its own forecast")
        require(not sources or set(sources) == allowed_sources, "unauthorized transfer source")
        for ref in all_ids:
            r = rows[ref]
            require(
                timestamp(r["recorded_at"]) < timestamp(plan["created_at"])
                and feasible(r, study["spec"]["quality"]),
                "future or infeasible training result",
            )
            require(
                (ref in train and r["study_ref"] == study["ref"] and r["mode"] == "pilot")
                or (ref in sources and r["mode"] == "confirmation"),
                "training scope mismatch",
            )
        p = forecasts[0]
        mean, interval = p.get("predicted_elapsed_seconds"), p.get("posterior_interval_seconds")
        if not (
            p.get("measured") is False
            and finite(mean, positive=True)
            and isinstance(interval, list)
            and len(interval) == 2
            and all(finite(v, positive=True) for v in interval)
            and interval[0] <= mean <= interval[1]
        ):
            excluded.append({"attempt_id": row["attempt_id"], "reason": "INVALID_FORECAST"})
            continue
        actual = row["measurements"]["elapsed_seconds"]
        accepted.append(
            {
                "attempt_id": row["attempt_id"],
                "study_ref": study["ref"],
                "strategy": study["request"]["strategy"],
                "plan_ref": plan["ref"],
                "forecast_at": plan["created_at"],
                "target_backend_submitted_at": row["submitted_at"],
                "target_training_ids": train,
                "source_training_ids": sources,
                "predicted_seconds": mean,
                "interval_seconds": interval,
                "actual_seconds": actual,
                "covered": interval[0] <= actual <= interval[1],
                "width_seconds": interval[1] - interval[0],
                "absolute_relative_error": abs(mean - actual) / actual,
                "constraint_violation": not feasible(row, study["spec"]["quality"]),
                "whole_workload_holdout": False,
            }
        )
    groups = {}
    for strategy in sorted({r["strategy"] for r in accepted}):
        group = [r for r in accepted if r["strategy"] == strategy]
        groups[strategy] = {
            "count": len(group),
            "study_count": len({r["study_ref"] for r in group}),
            "covered": sum(r["covered"] for r in group),
            "coverage_fraction": statistics.mean(r["covered"] for r in group),
            "mean_width_seconds": statistics.mean(r["width_seconds"] for r in group),
            "mean_absolute_relative_error": statistics.mean(
                r["absolute_relative_error"] for r in group
            ),
            "constraint_violations": sum(r["constraint_violation"] for r in group),
        }
    return {
        "groups": groups,
        "accepted": accepted,
        "excluded": excluded,
        "exclusion_counts": dict(Counter(r["reason"] for r in excluded)),
        "semantics": "Saved latent posterior intervals vs later completed Jobs; not calibrated prediction intervals",
    }


def evaluate(report):
    audited = summarize(report)
    rows = validate_rows(report)
    cohorts, semantic_ids, invariant = {}, set(), None
    labels = [f"source-{i}" for i in range(len(report["plan"]["sources"]))] + ["oracle"]
    reference = report["studies"]["oracle"]["spec"]
    cpus = {c["ref"]: c["context"]["resources"]["host_cpu"] for c in reference["candidates"]}
    lo, hi = min(cpus.values()), max(cpus.values())
    options = [
        {"candidate_ref": ref, "coordinates": [(value - lo) / (hi - lo)]}
        for ref, value in sorted(cpus.items())
    ]
    for label in labels:
        study = report["studies"][label]
        spec, identity = study["spec"], study["spec"]["identity"]
        semantic = signature({k: v for k, v in identity.items() if k not in {"seed", "work_units"}})
        require(semantic not in semantic_ids, "aliases or repetitions are not distinct workloads")
        semantic_ids.add(semantic)
        common = {
            "identity": {
                k: v
                for k, v in identity.items()
                if k not in {"input_shape", "dataset_version", "seed"}
            },
            "candidates": [
                {"ref": c["ref"], "backend": c["backend"], "context": c["context"]}
                for c in spec["candidates"]
            ],
            "quality": spec["quality"],
        }
        require(
            invariant is None or invariant == common,
            "unqualified workload family or runtime change",
        )
        invariant = common
        cohort = [
            rows[o["attempt_id"]] for o in study["observations"] if o["mode"] == "confirmation"
        ]
        require(
            Counter(r["candidate_ref"] for r in cohort)
            == {ref: reference["quality"]["minimum_repeats"] for ref in cpus},
            "incomplete or enlarged held-out grid",
        )
        cohorts[label] = {
            "signature": signature(identity),
            "shape": identity["input_shape"],
            "rows": cohort,
        }
    for row in report["source_evidence"]["provenance"]:
        captured = rows[row["attempt_id"]]
        require(
            row["result_digest"] == captured["result_digest"]
            and row["job_id"] == captured["job_id"]
            and row["workload_ref"] == captured["workload_ref"],
            "source profile/result binding mismatch",
        )
    runtime_groups = {s["runtime_group_signature"] for s in report["source_evidence"]["sources"]}
    require(len(runtime_groups) == 1, "multiple runtime groups")
    folds = [
        rank_fold(
            cohorts,
            label,
            runtime_group=next(iter(runtime_groups)),
            options=options,
            quality=reference["quality"],
        )
        for label in labels
    ]
    return {
        "schema_version": "workload-holdout-v1",
        "source_capture_digest": signature(report),
        "analysis_status": "RETROSPECTIVE_DESCRIPTIVE",
        "new_gpu_jobs": 0,
        "reused_source_characterization_cost": audited["source_characterization_cost"],
        "reused_application_gpu_reservation_seconds": sum(
            r["allocated_device_seconds"] for r in rows.values()
        ),
        "cost_scope": "Existing 157 application Jobs; qualification is reported in transfer-gpu.md; no savings claimed",
        "whole_workload_rank_holdout": folds,
        "chronological_forecasts": time_forecasts(report, rows),
        "limits": [
            "Three input shapes of one generated CNN on one GPU, not three model families",
            "All repetitions of a shape stay in one fold; source folds overlap and are not independent",
            "Rank prior only: the first probe is not a confirmed production recommendation",
            "All folds are retrospective; only source_precedes_target folds also preserve chronology",
            "RGPE/time forecasts use target pilots and cannot establish zero-shot workload interval coverage",
            "No refitting, hyperparameter selection, p-values, superiority or operational coverage guarantee",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must not already exist")
    result = evaluate(json.loads(args.input.read_text()))
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
