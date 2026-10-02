"""Noise-aware replication at fixed work units; not multi-fidelity BO.

RSE is a descriptive stopping heuristic, not a sequential confidence interval.
One independent attempt supplies one value, regardless of inner sample_count.
"""

import math
import random
import statistics
import time


def ask_replication(candidates, observations, quality, policy, seed):
    start = time.monotonic()
    ids = [o.get("attempt_id") for o in observations]
    if any(not ref for ref in ids) or len(ids) != len(set(ids)):
        raise ValueError("replication requires unique independent attempt IDs")
    summaries, excluded = [], {}
    for candidate in sorted(candidates, key=lambda c: c.ref):
        runs = [o for o in observations if o["candidate_ref"] == candidate.ref]
        values = []
        for obs in runs:
            m = obs.get("measurements")
            if (
                obs.get("outcome") != "COMPLETED"
                or not m
                or not math.isfinite(m["elapsed_seconds"])
                or m["elapsed_seconds"] <= 0
                or not math.isfinite(m["quality_value"])
                or m["quality_value"] < quality.minimum
                or not math.isfinite(m["peak_memory_mib"])
                or m["peak_memory_mib"] > quality.maximum_peak_memory_mib
            ):
                excluded[candidate.ref] = "FAILED_OR_INFEASIBLE_REPEAT"
                break
            values.append(m["elapsed_seconds"])
        if candidate.ref in excluded:
            continue
        mean = statistics.mean(values) if values else None
        rse = statistics.stdev(values) / math.sqrt(len(values)) / mean if len(values) > 1 else None
        summaries.append(
            {
                "candidate_ref": candidate.ref,
                "independent_runs": len(values),
                "mean_seconds": mean,
                "relative_standard_error": rse,
                "target_met": len(values) >= policy.minimum_runs
                and rse <= policy.target_relative_standard_error,
                "attempt_ids": [o["attempt_id"] for o in runs],
            }
        )
    rng = random.Random(seed + len(observations))
    under_minimum = [s for s in summaries if s["independent_runs"] < policy.minimum_runs]
    unresolved = [s for s in summaries if not s["target_met"]]
    repeatable = [s for s in unresolved if s["independent_runs"] < policy.maximum_runs]
    chosen, reason = None, "REPLICATION_PRECISION_TARGET_MET"
    if under_minimum:
        least = min(s["independent_runs"] for s in under_minimum)
        chosen = rng.choice([s for s in under_minimum if s["independent_runs"] == least])
        reason = "REPLICATION_BALANCED_INITIAL_RUNS"
    elif repeatable:
        # Estimated relative variance reduction from one more independent run.
        # No claim of optimal information gain or monetary savings.
        gains = {
            s["candidate_ref"]: s["relative_standard_error"] ** 2 / (s["independent_runs"] + 1)
            for s in repeatable
        }
        best = max(gains.values())
        chosen = rng.choice([s for s in repeatable if gains[s["candidate_ref"]] == best])
        reason = "REPLICATION_LARGEST_ESTIMATED_VARIANCE_REDUCTION"
    elif unresolved:
        reason = "REPLICATION_PER_CANDIDATE_LIMIT"
    elif not summaries:
        reason = "REPLICATION_NO_FEASIBLE_CANDIDATE"
    return {
        "candidate_ref": chosen["candidate_ref"] if chosen else None,
        "reason": reason,
        "stop_exploration": chosen is None,
        "statistics": summaries,
        "excluded": excluded,
        "policy": policy.model_dump(mode="json"),
        "statistical_scope": "descriptive RSE; no sequential coverage or rank guarantee",
        "multi_fidelity": False,
        "measured": False,
        "planning_seconds": time.monotonic() - start,
    }
