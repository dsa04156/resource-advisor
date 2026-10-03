"""Register a separate policy trial after a verified optimizer-runtime repair.

Preserve the first protocol and its costs. This does not resume any stopped study,
choose a new search space from observed outcomes, or submit work.
"""

import argparse
import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def digest(value):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def make_plan(original, stopped, repair):
    if stopped["status"] != "stopped" or not stopped["trial_must_not_resume"]:
        raise ValueError("predecessor must be terminal and retained")
    if stopped["reason"] != "WORKER_OPTIMIZER_DEPENDENCIES_MISSING":
        raise ValueError("this protocol is only for the recorded packaging repair")
    if (
        not repair["optimizer_required"]
        or "optimizer" not in repair["required_extras"]
        or repair["actual_model_check"]["reason"] != "CONSTRAINED_QLOGNEI"
        or repair["stopped_experiment_resumed"]
        or repair["new_gpu_jobs"] != 0
    ):
        raise ValueError("actual repaired-runtime qualification is required")
    costs = [
        stopped["application_gpu_reservation_seconds"],
        stopped["qualification_gpu_reservation_seconds"],
    ]
    if any(
        type(value) not in {int, float} or not math.isfinite(value) or value < 0 for value in costs
    ):
        raise ValueError("known nonnegative predecessor allocation costs required")
    prior_gpu = sum(costs)
    if (
        prior_gpu <= 0
        or stopped["results_ledger_api_s3_mlflow_verified"] != stopped["application_jobs"]
    ):
        raise ValueError("complete measured predecessor accounting is required")
    result = copy.deepcopy(original)
    result.update(
        schema_version="v2",
        created_at=datetime.now(timezone.utc).isoformat(),
        trial="new-policy-trial-after-optimizer-runtime-repair",
        predecessor={
            "plan_digest": digest(original),
            "stop_report_digest": digest(stopped),
            "repair_report_digest": digest(repair),
            "reason": stopped["reason"],
            "retained_application_jobs": stopped["application_jobs"],
            "retained_qualification_jobs": stopped["qualification_jobs"],
            "retained_gpu_reservation_seconds": prior_gpu,
            "policy": "Retain the incomplete predecessor separately. Never merge its observations into this comparison or hide its cost. Report cumulative project cost including both trials.",
        },
        runtime_requirements={
            "worker_image_digest": repair["worker_image_digest"],
            "worker_source_tree_sha256": repair["worker_source_tree_sha256"],
            "optimizer_required": True,
            "torch_version": repair["actual_model_check"]["torch_version"],
            "botorch_version": repair["actual_model_check"]["botorch_version"],
            "preflight": "Check the actual mounted worker configuration, image/source digest, and a bounded qLogNEI calculation before new F0 or study submission. This software check never becomes a trial observation.",
            "failure_policy": "Stop on changed worker image or missing optimizer dependency. Preserve and report numerical model fallback as a policy outcome; never relabel fallback as a BO acquisition or replace it.",
        },
        design_preservation="Same original seeds, temporal-block order, candidates, workload, quality gates, sample-size rationale and budgets. Fresh F0, fresh history, fresh study/attempt IDs; no old measurements supplied to any policy.",
    )
    return result


if __name__ == "__main__":
    root = Path(__file__).parents[1] / "docs/evidence"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-plan", type=Path, default=root / "policy-comparison-plan.json")
    parser.add_argument("--stop-report", type=Path, default=root / "policy-stop.json")
    parser.add_argument("--repair-report", type=Path, default=root / "optimizer-repair.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = make_plan(
        json.loads(args.original_plan.read_text()),
        json.loads(args.stop_report.read_text()),
        json.loads(args.repair_report.read_text()),
    )
    with args.output.open("x") as target:
        json.dump(plan, target, indent=2)
        target.write("\n")
