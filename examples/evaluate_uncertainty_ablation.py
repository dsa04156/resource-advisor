"""Offline S5 confirmation-gate replay; never submit work or relax quality/memory."""

import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from evaluate_policy_comparison import feasible, require, summarize


def replay(study):
    """Change only uncertainty-based baseline retention on the recorded finalists."""
    rec = study.get("recommendation")
    if not rec:
        return {
            "strict": None,
            "without_uncertainty": None,
            "reason": "EXISTING_ABSTENTION_RETAINED",
            "ranking": [],
            "rejected": [],
        }
    spec = study["spec"]
    baseline = spec["baseline_candidate_ref"]
    candidates = {c["ref"] for c in spec["candidates"]}
    confirmations = [o for o in study["observations"] if o["mode"] == "confirmation"]
    ids = [o["attempt_id"] for o in confirmations]
    require(len(ids) == len(set(ids)), "duplicate confirmation attempt")
    require(set(ids) == set(rec["confirmation_run_ids"]), "confirmation cohort changed")
    require(
        all(
            o["candidate_ref"] in candidates
            and datetime.fromisoformat(study["created_at"])
            <= datetime.fromisoformat(o["recorded_at"])
            <= datetime.fromisoformat(rec["created_at"])
            for o in confirmations
        ),
        "unapproved candidate or future confirmation",
    )
    ranking, rejected = [], []
    for ref in sorted({o["candidate_ref"] for o in confirmations}):
        runs = [o for o in confirmations if o["candidate_ref"] == ref]
        if len(runs) < spec["quality"]["minimum_repeats"] or not all(
            feasible(o, spec["quality"]) for o in runs
        ):
            rejected.append(ref)
            continue
        values = [o["measurements"]["elapsed_seconds"] for o in runs]
        mean = statistics.mean(values)
        radius = 3 * statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else mean
        ranking.append(
            {
                "candidate_ref": ref,
                "mean_seconds": mean,
                "sample_sd_seconds": statistics.stdev(values) if len(values) > 1 else None,
                "heuristic_interval_seconds": [max(0, mean - radius), mean + radius],
                "confirmation_attempt_ids": [o["attempt_id"] for o in runs],
                "raw_seconds": values,
            }
        )
    ranking.sort(
        key=lambda r: (r["mean_seconds"], r["candidate_ref"] != baseline, r["candidate_ref"])
    )
    original = next((r for r in ranking if r["candidate_ref"] == baseline), None)
    if original is None or rec["predicted_candidate"] in rejected:
        return {
            "strict": None,
            "without_uncertainty": None,
            "reason": "QUALITY_MEMORY_OR_REPEAT_GATE",
            "ranking": ranking,
            "rejected": rejected,
        }
    chosen = ranking[0]
    overlap = chosen["heuristic_interval_seconds"][1] >= original["heuristic_interval_seconds"][0]
    strict = baseline if chosen != original and overlap else chosen["candidate_ref"]
    require(strict == rec["confirmed_candidate"], "saved selection differs from strict replay")
    return {
        "strict": strict,
        "without_uncertainty": chosen["candidate_ref"],
        "reason": "RECORDED_FINALISTS_ONLY",
        "ranking": ranking,
        "rejected": rejected,
    }


def evaluate(report, *, predecessor=None):
    # Also establishes chronology, independent later reference runs and full costs.
    audited = summarize(report, predecessor=predecessor)
    reference = audited["oracle_confirmation_mean_seconds"]
    best = min(reference.values())
    decisions = []
    for item in audited["studies"]:
        study = report["studies"][item["label"]]
        result = replay(study)
        baseline = study["spec"]["baseline_candidate_ref"]
        decisions.append(
            {
                "label": item["label"],
                "block": item["block"],
                "strategy": item["strategy"],
                "study_ref": study["ref"],
                "baseline": baseline,
                **result,
                "decision_changed": result["strict"] != result["without_uncertainty"],
                "later_reference": {
                    mode: {
                        "mean_seconds": reference[chosen],
                        "selection_regret_fraction": reference[chosen] / best - 1,
                        "relative_to_baseline": reference[chosen] / reference[baseline] - 1,
                    }
                    if (chosen := result[mode]) is not None
                    else None
                    for mode in ("strict", "without_uncertainty")
                },
                "original_study_gpu_reservation_seconds": item["allocated_gpu_seconds"],
            }
        )
    denominator = len(decisions)
    rates = {}
    for mode in ("strict", "without_uncertainty"):
        available = sum(d[mode] is not None for d in decisions)
        changes = sum(d[mode] is not None and d[mode] != d["baseline"] for d in decisions)
        rates[mode] = {
            "decision_count": available,
            "abstention_count": denominator - available,
            "nonbaseline_count": changes,
            "decision_coverage": available / denominator,
            "nonbaseline_recommendation_fraction": changes / denominator,
            "denominator_studies": denominator,
        }
    return {
        "schema_version": "offline-uncertainty-ablation-v1",
        "scope": "Exploratory offline replay of the final confirmation uncertainty gate; not a new trial or a new optimizer trajectory",
        "source_capture_digest": "sha256:"
        + hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "decisions": decisions,
        "rates": rates,
        "changed_decisions": sum(d["decision_changed"] for d in decisions),
        "new_gpu_jobs": 0,
        "measurement_cost_reused_not_saved": audited["total_gpu_reservation_seconds"],
        "reference_mean_seconds": reference,
        "quality_memory_and_repeat_checks_preserved": True,
        "cross_workload_holdout": "NOT_QUALIFIED: this capture contains one logical workload",
        "limitations": [
            "Finalists and observations come from the executed policy; this does not estimate how removing uncertainty throughout search would change its trajectory.",
            "The original policy can retain the baseline while returning a valid recommendation; baseline retention is not counted as missing coverage.",
            "Later independent reference means are finite-sample estimates on the same GPU, not true error labels or a causal trial of S5 execution.",
            "The replay uses the original mean +/- 3 sample SD / sqrt(n) heuristic; it is not a calibrated 95% interval.",
            "Untested finalists, failed quality/memory checks and existing abstentions are not promoted into recommendations.",
            "No significance tests, superiority claims, new measurements, cost savings or workload-generalization claims.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--predecessor-stop", type=Path, default=Path("docs/evidence/policy-stop.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.capture.read_text())
    require(report["plan"] == json.loads(args.plan.read_text()), "capture differs from frozen plan")
    prior = (
        json.loads(args.predecessor_stop.read_text()) if "predecessor" in report["plan"] else None
    )
    result = evaluate(report, predecessor=prior)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
