"""Freeze a bounded S0/S1/S2 GPU protocol before collecting any trial data."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import brentq
from scipy.stats import nct, t


def make_plan(seed=2026100301):
    rng = np.random.default_rng(seed)
    schedule = [
        {"order": 1 + block * 3 + i, "block": block, "strategy": arm, "seed": seed + block}
        for block in range(3)
        for i, arm in enumerate(rng.permutation(["lookup", "random", "qlognei"]).tolist())
    ]
    alpha, n = 0.05 / 3, 3
    critical = t.ppf(1 - alpha / 2, n - 1)

    def power(d):
        nc = d * n**0.5
        # -T(df,nc) has T(df,-nc): use both survival tails. Direct negative
        # CDF evaluation returned NaN for finite inputs in the pinned SciPy.
        return float(nct.sf(critical, n - 1, nc) + nct.sf(critical, n - 1, -nc))

    # Verify the bracket before solving the illustrative sensitivity calculation.
    assert power(0) < 0.8 < power(10)
    mde = brentq(lambda d: power(d) - 0.8, 0, 10)
    return {
        "schema_version": "v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "scope": "prospective repeated S0/S1/S2 GPU comparison; not a powered superiority study",
        "question": "Which feasible configuration does each policy confirm, and what does history, exploration and confirmation cost?",
        "workload": {
            "runner": "resource_advisor.transfer_gpu_benchmark",
            "input_shape": [1, 3, 256, 256],
            "seed": 20261003,
            "work_units": 12,
            "forwards_per_unit": 32,
            "warmup": 3,
            "precision": "fp32",
            "task": "generated fixed-weight CNN numerical inference; not trained-model accuracy",
        },
        "candidate_cpu_cores": [0.5, 1, 2],
        "gpu_count": 1,
        "host_memory_mib": 2048,
        "baseline": "cpu1",
        "cpu_threads": "max(1,int(requested_cpu)); fractional limit enforced by cgroup",
        "quality": {
            "numerical_agreement_minimum": 1.0,
            "maximum_peak_memory_mib": 2048,
            "independent_confirmation_repeats": 3,
        },
        "history": {
            "strategy": "grid_characterization",
            "probe_blocks": 2,
            "confirmation_repeats_per_candidate": 3,
            "frozen_lookup_profile_count": 9,
            "policy": "Freeze this new same-workload cohort before all comparisons. S0 uses only these IDs/digest; later results and oracle are excluded. S1/S2 deliberately start without historical observations. Report that information difference and the full cost to acquire S0 history.",
        },
        "schedule": schedule,
        "randomization": {
            "method": "complete blocks; NumPy default_rng(seed).permutation(arms) per block",
            "numpy_version": np.__version__,
            "unit": "policy study within temporal block; confirmation Jobs nested; forward iterations are subsamples",
        },
        "budgets": {
            "per_study_max_probes": 8,
            "per_study_wall_seconds": 1200,
            "per_study_device_seconds": 900,
            "final_validation_reserved_seconds": 360,
            "queue_seconds": 90,
            "run_seconds": 30,
            "collection_seconds": 60,
            "whole_protocol_wall_seconds": 7200,
            "policy": "Equal first-use total caps: 1200 wall seconds and 900 physical GPU reservation seconds, including S0 history acquisition plus its target study. Before S0, require measured history cost plus the 360-second confirmation reserve to fit both caps; otherwise stop. Later reuse never double-charges history. S1/S2 have no historical acquisition cost. Caps are not equal realized spending.",
        },
        "maximum_jobs": {
            "qualification": 3,
            "history": 15,
            "lookup": 18,
            "random": 42,
            "qlognei": 42,
            "oracle": 15,
            "total": 135,
        },
        "oracle": {
            "strategy": "grid_characterization",
            "when": "after every comparison is terminal",
            "use": "evaluation only; unavailable to historical or online selection",
        },
        "analysis": {
            "primary": "raw block-paired chosen-configuration oracle regret and independent confirmation",
            "secondary": [
                "quality failures, abstention and baseline preservation",
                "queue, execution, collection and study wall time",
                "GPU reservation seconds by history, tuning, confirmation, oracle and F0",
                "optimizer planning time",
                "first-use cost including history; reuse across the three observed blocks",
                "no production break-even claim without measured comparable repeated savings",
            ],
            "inference": "descriptive block differences/ranges and all failures; no p-value superiority claim",
            "sample_size_basis": "bounded operational comparison; repetitions are not independent devices/workloads",
            "illustrative_paired_t_sensitivity": {
                "assumptions": "independent normal block differences; illustrative, not the planned inferential analysis",
                "blocks": n,
                "two_sided_alpha_three_pairs": alpha,
                "target_power": 0.8,
                "standardized_difference_mde": mde,
                "power_by_standardized_difference": {str(d): power(d) for d in [0.5, 1, 2, 5]},
            },
        },
        "failure_policy": "Retain every failure/abstention. Stop on pressure, lost qualification, failed thermal eligibility or protocol deadline. Never expand quota, extend budget, relax gates or replace failed runs after seeing results.",
        "limits": [
            "one GPU and one numerical workload; no cross-accelerator superiority",
            "three meaningful candidates; no artificial space inflation",
            "same code/data/output contract; no training convergence claim",
            "NVML brackets are not continuous energy measurement",
            "realized cost may differ with selected confirmation path",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = make_plan()
    with args.output.open("x") as target:
        json.dump(plan, target, indent=2)
        target.write("\n")
