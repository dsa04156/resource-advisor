"""Audited whole-shape numerical shadow holdout, with out-of-range abstention."""

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

from evaluate_workload_holdout import evaluate as audit_capture

from resource_advisor.contracts import signature
from resource_advisor.workload_uncertainty import SourceCell, Target, forecast


def prepare_capture(report):
    audited = audit_capture(report)
    folds = audited["whole_workload_rank_holdout"]
    reference = report["studies"][folds[0]["held_out"]]["spec"]
    family = signature(
        {
            "identity": {
                k: v
                for k, v in reference["identity"].items()
                if k not in {"input_shape", "dataset_version", "seed"}
            },
            "candidates": [
                {"ref": c["ref"], "backend": c["backend"], "context": c["context"]}
                for c in reference["candidates"]
            ],
            "quality": reference["quality"],
        }
    )
    cohorts = {}
    for fold in folds:
        spec = report["studies"][fold["held_out"]]["spec"]
        shape = spec["identity"]["input_shape"]
        if (
            len(shape) != 4
            or shape[:2] != [1, 3]
            or shape[2] != shape[3]
            or spec["identity"]["dataset_version"] != f"generated-cnn-normal-{shape[2]}-v1"
        ):
            raise ValueError("analysis only supports the captured generated square-input family")
        cpus = {c["ref"]: c["context"]["resources"]["host_cpu"] for c in spec["candidates"]}
        cohorts[fold["held_out"]] = [
            SourceCell(
                descriptor=Target(
                    fold["workload_signature"], family, ref, shape[2] * shape[3], cpus[ref]
                ),
                attempt_ids=tuple(samples["attempt_ids"]),
                elapsed_seconds=tuple(samples["raw_seconds"]),
            )
            for ref, samples in fold["held_out_samples"].items()
        ]
    return audited, cohorts


def score_targets(predictions, target_cells, raw_rows, quality):
    """Only scoring receives outcomes; fitting sees target descriptors alone."""
    by_candidate = {r["descriptor"]["candidate_ref"]: r for r in predictions["targets"]}
    scored, excluded = [], []
    for cell in target_cells:
        target = by_candidate[cell.descriptor.candidate_ref]
        for attempt, actual in zip(cell.attempt_ids, cell.elapsed_seconds, strict=True):
            row = raw_rows[attempt]
            m = row["measurements"]
            base = {
                "attempt_id": attempt,
                "candidate_ref": cell.descriptor.candidate_ref,
                "actual_seconds": actual,
                "constraint_violation": (
                    row["outcome"] != "COMPLETED"
                    or m["quality_value"] < quality["minimum"]
                    or m["peak_memory_mib"] > quality["maximum_peak_memory_mib"]
                ),
            }
            if target["prediction"] is None:
                excluded.append({**base, "reasons": target["reasons"]})
                continue
            p = target["prediction"]
            scored.append(
                {
                    **base,
                    "predicted_median_seconds": p["median_seconds"],
                    "absolute_relative_error": abs(actual - p["median_seconds"]) / actual,
                    **{
                        kind: {
                            "interval_seconds": p[f"{kind}_interval_seconds"],
                            "covered": p[f"{kind}_interval_seconds"][0]
                            <= actual
                            <= p[f"{kind}_interval_seconds"][1],
                            "width_seconds": p[f"{kind}_interval_seconds"][1]
                            - p[f"{kind}_interval_seconds"][0],
                        }
                        for kind in ("latent", "job")
                    },
                }
            )
    return {
        "scored": scored,
        "excluded": excluded,
        "summary": {
            "target_jobs": len(scored) + len(excluded),
            "predicted_jobs": len(scored),
            "abstained_jobs": len(excluded),
            "constraint_violations_all_completed_targets": sum(
                r["constraint_violation"] for r in [*scored, *excluded]
            ),
            "mean_absolute_relative_error": statistics.mean(
                r["absolute_relative_error"] for r in scored
            )
            if scored
            else None,
            **{
                kind: {
                    "covered": sum(r[kind]["covered"] for r in scored),
                    "coverage_fraction": statistics.mean(r[kind]["covered"] for r in scored)
                    if scored
                    else None,
                    "mean_width_seconds": statistics.mean(r[kind]["width_seconds"] for r in scored)
                    if scored
                    else None,
                }
                for kind in ("latent", "job")
            },
        },
    }


def evaluate(report):
    audited, cohorts = prepare_capture(report)
    raw_rows = {r["attempt_id"]: r for r in report["observations"]}
    quality = report["studies"]["oracle"]["spec"]["quality"]
    folds = []
    for prior_fold in audited["whole_workload_rank_holdout"]:
        label = prior_fold["held_out"]
        target_cells = cohorts[label]
        source_cells = [
            cell for other, cells in cohorts.items() if other != label for cell in cells
        ]
        # No target durations or target normalization reach the forecasting boundary.
        predictions = forecast(source_cells, [c.descriptor for c in target_cells])
        scores = score_targets(predictions, target_cells, raw_rows, quality)
        folds.append(
            {
                "held_out": label,
                "input_shape": prior_fold["input_shape"],
                "workload_signature": prior_fold["workload_signature"],
                "source_precedes_target": prior_fold["source_precedes_target"],
                "latest_source_available_at": prior_fold["latest_source_available_at"],
                "earliest_target_backend_submitted_at": prior_fold[
                    "earliest_target_backend_submitted_at"
                ],
                "held_out_samples": prior_fold["held_out_samples"],
                "forecast": predictions,
                **scores,
            }
        )
    ids = {a for cells in cohorts.values() for cell in cells for a in cell.attempt_ids}
    excluded = [r["attempt_id"] for r in report["observations"] if r["attempt_id"] not in ids]
    return {
        "schema_version": "numerical-workload-holdout-v1",
        "analysis_status": "RETROSPECTIVE_EXPLORATORY_SHADOW",
        "source_capture_digest": signature(report),
        "new_gpu_jobs": 0,
        "execution_authorized": False,
        "source_application_jobs": len(report["observations"]),
        "source_application_gpu_reservation_seconds": audited[
            "reused_application_gpu_reservation_seconds"
        ],
        "balanced_grid_jobs": len(ids),
        "outside_grid_attempt_ids": excluded,
        "cost_scope": "Original 157 application Jobs; 38 qualification GPU seconds remain in transfer-gpu.md",
        "whole_workload_folds": folds,
        "prediction_status_counts": dict(
            Counter(r["status"] for fold in folds for r in fold["forecast"]["targets"])
        ),
        "chronological_saved_forecasts": audited["chronological_forecasts"],
        "limits": [
            "One CNN/runtime family and three shapes; correlated folds, not independent model families",
            "No operational use, automatic approval or qualification from this shadow model",
            "Out-of-source-range shapes abstain; abstentions are neither covered nor misses",
            "Pooled source noise is an uncalibrated transfer assumption, not predicted target noise",
            "Retrospective fit does not backfill a forecast saved before execution",
            "Chronological forecasts belong to existing workload-scoped models, not this new shadow GP",
            "No retuning, outlier removal, model selection, p-values or 95% operational guarantee",
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
