"""Reproduce the bounded pool result; qualify slot-time and heterogeneous sensors."""

import argparse
import ast
import csv
import hashlib
import json
import math
import statistics
from pathlib import Path

from resource_advisor.profile_reuse_evaluation import summarize


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def sensor_groups(cohort):
    groups = []
    for job in cohort["jobs"]:
        points = job["details"]["sensor"]
        sources = sorted({point["source"] for point in points})
        integral = observed = 0
        for a, b in zip(points, points[1:], strict=False):
            dt = b["at"] - a["at"]
            if (
                0 < dt <= 1
                and a["source"] == b["source"]
                and all(point["utilization"] is not None and not point["error"] for point in (a, b))
            ):
                integral += dt * (a["utilization"] + b["utilization"]) / 2
                observed += dt
        groups.append(
            dict(
                attempt_id=job["attempt_id"],
                node=job["candidate_ref"],
                sources=sources,
                sampled_load_percent=integral / observed if observed else None,
                observed_seconds=observed,
                samples=len(points),
                scope="physical-device load in sampled execution windows; source-specific, no per-job attribution",
            )
        )
    return groups


def audit(directory):
    capture = json.loads((directory / "capture.json").read_text())
    source = json.loads((directory / "source.json").read_text())
    plan = capture["plan"]
    assert source["immutable"] is True
    assert sha(source["data"]["fixture.json"].encode()) == plan["fixture_sha256"]
    assert sha(source["data"]["workload.py"].encode()) == plan["source_sha256"]
    fixture = json.loads(source["data"]["fixture.json"])
    tree = ast.parse(source["data"]["workload.py"])
    ptx = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "PTX" for target in node.targets)
    )
    kernel_hash = sha(ptx)
    nodes = {n["ref"]: n for n in plan["pool"]}
    readback_path = directory / "pool-readback.json"
    slots = None
    if readback_path.exists():
        readback = json.loads(readback_path.read_text())
        allocatable = {n["name"]: n["allocatable"] for n in readback["nodes"]}
        slots = sum(int(allocatable[n["node"]][n["resource_key"]]) for n in plan["pool"])
    source_ids = {q["attempt_id"] for q in capture["qualifications"]}
    seen = set()
    rows = []
    for cohort in capture["cohorts"]:
        for job in cohort["jobs"]:
            detail = job["details"]
            assert job["attempt_id"] not in seen | source_ids
            seen.add(job["attempt_id"])
            assert detail["fixture_sha256"] == plan["fixture_sha256"]
            assert detail["kernel_sha256"] == kernel_hash
            assert detail["rounds"] == plan["main_rounds"]
            assert detail["images"] == plan["main_rounds"] * fixture["batch_size"]
            assert detail["quality"] == 1 and detail["accuracy"] == fixture["accuracy"]
            receipt = job["native_receipt"]
            node = nodes[job["candidate_ref"]]
            pod = receipt["pod"]
            assert receipt["termination"]["exitCode"] == 0
            assert pod["spec"]["nodeName"] == node["node"]
            assert pod["spec"]["containers"][0]["image"] == node["image"]
            assert (
                pod["spec"]["containers"][0]["resources"]["requests"][node["resource_key"]] == "1"
            )
            assert (job["profile_source"] in source_ids) == (cohort["arm"] == "reuse")
            assert job["gpu_seconds"] == receipt["finished_at"] - receipt["scheduled_at"]
            rows.append(
                dict(
                    arm=cohort["arm"],
                    attempt_id=job["attempt_id"],
                    candidate=job["candidate_ref"],
                    resource_key=node["resource_key"],
                    jct_seconds=job["finished_at"] - job["accepted_at"],
                    wait_seconds=job["started_at"] - job["accepted_at"],
                    accelerator_slot_seconds=job["gpu_seconds"],
                    synchronized_inference_seconds=detail["elapsed_seconds"],
                    images=job["units"],
                    quality=detail["quality"],
                    accuracy=detail["accuracy"],
                )
            )
    # Adapt the shortened pool-plan field to the existing cohort auditor's schema.
    capture["plan"] = dict(plan, jobs_per_cohort=plan["jobs_per_arm"])
    summary = summarize(capture)
    for cohort in capture["cohorts"]:
        arm = summary["arms"][cohort["arm"]]
        arm["accelerator_slot_seconds"] = arm.pop("gpu_seconds")
        arm["accelerator_slot_hours"] = arm.pop("gpu_hours")
        arm["physical_gpu_hours"] = None
        arm["slot_occupancy_percent"] = arm.pop("reservation_occupancy_percent")
        arm["nominal_pool_slot_occupancy_percent"] = (
            arm["slot_occupancy_percent"] / slots if slots else None
        )
        arm["synchronized_inference_seconds"] = math.fsum(
            j["details"]["elapsed_seconds"] for j in cohort["jobs"]
        )
        arm["mean_synchronized_inference_seconds"] = statistics.mean(
            j["details"]["elapsed_seconds"] for j in cohort["jobs"]
        )
        arm["sensors_by_native_job"] = sensor_groups(cohort)
        for key in list(arm):
            if key.startswith(("nvml_", "dcgm_")):
                del arm[key]
        arm["continuous_pool_gpu_utilization_percent"] = None
    summary["improvements_percent"] = {
        key: value
        for key, value in summary["improvements_percent"].items()
        if not key.startswith(("nvml", "dcgm", "reservation", "gpu_"))
    }
    a, b = (summary["arms"][arm] for arm in ("baseline", "reuse"))
    summary["improvements_percent"]["main_accelerator_slot_seconds"] = (
        1 - b["accelerator_slot_seconds"] / a["accelerator_slot_seconds"]
    ) * 100
    summary["improvements_percent"]["synchronized_inference_seconds"] = (
        1 - b["synchronized_inference_seconds"] / a["synchronized_inference_seconds"]
    ) * 100
    summary["profile_upfront"] = dict(
        accelerator_slot_seconds=capture["profiling"]["gpu_seconds"],
        wall_seconds=capture["profiling"]["wall_seconds"],
        failed_native_attempts=1,
        successful_microprofiles=len(source_ids),
        cpu_model_training_cost="shared preparation; not measured, not zero",
    )
    summary["net_cost"] = dict(
        baseline_slot_seconds=a["accelerator_slot_seconds"],
        reuse_with_profile_slot_seconds=b["accelerator_slot_seconds"]
        + capture["profiling"]["gpu_seconds"],
        nominal_slot_reduction_percent=summary.pop("net_gpu_improvement_percent"),
        observed_at_jobs=summary.pop("observed_gpu_payback_jobs"),
        established_physical_gpu_payback=False,
        interpretation="whole-second mixed accelerator-slot allocation proxy; no prices or exclusive physical GPU-hours verified",
        net_difference_timestamp_resolution_bound_seconds=16,
        net_difference_direction_established=False,
        baseline_wall_seconds=a["wall_seconds"],
        reuse_with_setup_wall_seconds=b["wall_seconds"] + capture["profiling"]["wall_seconds"],
        observed_wall_payback_jobs=summary.pop("observed_wall_payback_jobs"),
        net_wall_reduction_percent=summary.pop("net_wall_improvement_percent"),
    )
    # The original generic evaluator's gpu-labelled curve is retained only in private output.
    del summary["cumulative"]
    summary.update(
        policy_runs_per_arm=1,
        native_main_jobs_per_arm=5,
        physical_k8s_gpu_nodes=5,
        nominal_gpu_slots=slots,
        main_successes=len(rows),
        main_failures=0,
        unique_validation_images=256,
        reused_images_per_main_job=fixture["batch_size"] * plan["main_rounds"],
        accuracy=fixture["accuracy"],
        reference_agreement=1,
        slurm_native_jobs=0,
        slurm_status=capture["excluded_slurm"],
        p95_method="nearest rank over five Jobs per arm = maximum; descriptive only",
        utilization_scope="per-node NVML/sysfs sampled windows and native slot occupancy; continuous pool utilization unknown",
        deployed_production_scheduling_changed=False,
        limitations=[
            "single cohort; no confidence interval or repeatability claim",
            "mixed exclusive/shared accelerator-slot time is not verified physical GPU-hours",
            "native timestamps have whole-second resolution; net five-slot-second saving can reverse within quantization bound",
            "profile work-size scaling only informed placement; profile/main source digests identical",
            "fixture contains256 actual held-out images repeatedly inferred; no model training speedup",
            "Slurm controller unavailable; this is not a complete Kubernetes+Slurm comparison",
        ],
    )
    return summary, rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result, rows = audit(args.directory)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    with (args.output / "main.csv").open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(
        json.dumps(
            dict(
                output=str(args.output),
                main_successes=result["main_successes"],
                improvements=result["improvements_percent"],
            )
        )
    )
