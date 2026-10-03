"""Freeze the B0/B1/B2 operational comparison before any new GPU observations."""

import argparse
import json
from pathlib import Path


def make_plan(seed=20261004, blocks=6):
    import numpy as np

    if type(blocks) is not int or not 3 <= blocks <= 6:
        raise ValueError("three to six bounded descriptive blocks required")
    rng = np.random.default_rng(seed)
    schedule = []
    for block in range(blocks):
        arms = ["B0", "B1", "B2"]
        rng.shuffle(arms)
        schedule.extend({"block": block, "arm": arm} for arm in arms)
    return {
        "schema_version": "operational-comparison-v1",
        "seed": seed,
        "blocks": blocks,
        "schedule": schedule,
        "allocation": "permuted blocks of three, numpy default_rng shuffle, one of each arm",
        "sample_size_basis": "bounded descriptive operational trial; six paired blocks, one GPU/workload and one tuning study; no powered superiority claim",
        "arms": {
            "B0": "qualified fixed cpu1 Kubernetes Job through the same Kueue queue; direct submission and raw log collection; no Advisor API/history/approval",
            "B1": "same fixed cpu1 through Advisor observe mode; compatibility/identity checks, validated result, PostgreSQL and artifact/MLflow recording; no recommendation applied",
            "B2": "same platform path with one fresh qlognei study, independent confirmation, immutable approval and fixed-mode approved candidate",
        },
        "workload": {
            "model": "generated fixed-weight three-convolution CNN",
            "seed": seed,
            "input_shape": [1, 3, 256, 256],
            "precision": "fp32",
            "work_units": 12,
            "forwards_per_unit": 32,
            "warmup": 3,
            "quality": "numerical_agreement=1; not task accuracy",
            "host_memory_mib": 2048,
            "gpu_count": 1,
            "baseline_cpu": 1,
            "candidate_cpu_cores": [0.5, 1, 2],
        },
        "profiling": {
            "strategy": "qlognei",
            "seed": seed,
            "max_probes": 8,
            "max_confirmation_jobs": 6,
            "minimum_repeats": 3,
            "total_wall_seconds": 1200,
            "physical_gpu_seconds": 900,
            "final_validation_seconds": 360,
            "history": "fresh workload/model identity; no external history; no main-run outcomes available during tuning",
        },
        "budgets": {
            "qualification_jobs": 3,
            "profiling_jobs_maximum": 14,
            "main_jobs": blocks * 3,
            "total_jobs_maximum": 17 + blocks * 3,
            "main_job_run_seconds": 30,
            "qualification_job_run_seconds": 90,
            "queue_seconds": 90,
            "collection_seconds": 60,
            "whole_protocol_wall_seconds": 3600,
            "whole_protocol_physical_gpu_seconds": 1500,
        },
        "fixed_environment": {
            "device": "one physical RTX 5080; no shared-GPU or cross-scheduler comparison",
            "quota_cpu": 2,
            "quota_physical_gpu": 1,
            "worker_image_digest": "sha256:71b21cd269fbee835457dfdd4960ba4369388e3a74274009294cc569ed950e2f",
            "poll_seconds": 2,
            "common_observation": "same workload/phase/NVML instrumentation and scheduler timestamps for all arms; direct raw collection for all arms",
            "platform_collection": "B1/B2 additionally await validated API result and S3/MLflow delivery; report separately from backend completion",
            "cache": "qualification warms runtime/image; no artificial cache flush or claim of cold-start image performance",
        },
        "responses": [
            "request-to-raw-result wall seconds, with equal external observation cadence",
            "B1/B2 request-to-validated-result and request-to-delivery wall seconds",
            "queue, preparation, container duration, measured compute boundary, collection",
            "physical GPU reservation seconds, CPU core reservation seconds, host-memory MiB-seconds separately",
            "all quality/memory/failure/abstention outcomes; no silent replacement",
            "explicit NVML query overhead and external observation-call wall time; not total instrumentation counterfactual",
            "paired B1-minus-B0 and B2-minus-B1 main-run differences, descriptive only",
            "first-use and N=1..blocks serial cumulative cost with all B2 tuning/confirmation/approval costs charged once",
        ],
        "cost_boundaries": {
            "shared_qualification": "show all three F0 costs separately; shared setup is not free and is not attributed only to B2",
            "B2_first_use": "entire profiling study plus recommendation/approval preparation plus first independent main run",
            "repeat_use": "actual completed main runs only; no extrapolated break-even or assumed future speedup",
            "serial_definition": "sum durations of each arm's runs plus its setup; not interleaved trial makespan or parallel capacity",
            "validation_effect": "B1 vs B0 is the full added platform path, not a causal microbenchmark of just the validation function",
        },
        "stop_rules": [
            "node NotReady/pressure, worker image drift, unknown allocation cost, thermal gate failure or changed queue quota",
            "retain failed or abstained profiling; B2 falls back to explicitly labeled unchanged baseline without inventing approval",
            "retain every failed main run; no retry with a new identity to replace a poor result",
            "an observation timeout is not termination; reconcile saved backend/API IDs before any further submission",
            "stop new work at global wall/device limit; retain all evidence and classify incompleteness",
            "do not extend approval/capability expiry or relax confirmation/quality/compatibility gates",
        ],
        "limits": [
            "one workload and one device, with one B2 tuning episode reused across blocks",
            "baseline cpu1 is reasonable; cpu2 can use more CPU for little or no measured improvement",
            "B0 direct client and B1/B2 worker have different control-plane paths; decompose them rather than label every difference validator overhead",
            "scheduler timestamps have whole-second resolution; zero recorded queue seconds is not exactly zero waiting",
            "no integrated energy or job-attributed GPU utilization inferred from bracketing telemetry",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as stream:
        stream.write(json.dumps(make_plan(), indent=2) + "\n")


if __name__ == "__main__":
    main()
