"""Audit a completed prospective S0/S1/S2 capture without fitting or submitting work."""

import argparse
import copy
import hashlib
import itertools
import json
import math
import statistics
from datetime import datetime
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def seconds(value):
    require(value is not None and math.isfinite(value) and value >= 0, "unknown/invalid cost")
    return value


def feasible(observation, quality):
    m = observation.get("measurements")
    return (
        observation["outcome"] == "COMPLETED"
        and m is not None
        and m["quality_value"] >= quality["minimum"]
        and 0 <= m["peak_memory_mib"] <= quality["maximum_peak_memory_mib"]
        and math.isfinite(m["elapsed_seconds"])
        and m["elapsed_seconds"] > 0
    )


def comparable(spec):
    value = copy.deepcopy(spec)
    value.pop("ref")
    value["profiling"].pop("total_wall_seconds")
    value["profiling"].pop("device_seconds")
    for candidate in value["candidates"]:
        candidate.pop("variant_ref")
    return value


def summarize(report, *, predecessor=None):
    require(report["phase"] == "completed", "protocol is not complete")
    studies, design, rows = report["studies"], report["plan"], report["observations"]
    schedule, caps = design["schedule"], design["budgets"]
    labels = [f"target-{slot['block']}-{slot['strategy']}" for slot in schedule]
    require(len(labels) == len(set(labels)) == 9, "expected nine unique scheduled studies")
    require([s["order"] for s in schedule] == list(range(1, 10)), "invalid schedule order")
    require(
        all(
            sorted(s["strategy"] for s in schedule if s["block"] == block)
            == ["lookup", "qlognei", "random"]
            for block in range(3)
        ),
        "incomplete temporal block",
    )
    require(set(studies) == {"history", "oracle", *labels}, "unexpected or missing study")
    require(
        all(
            s["state"] in {"COMPLETED", "ABSTAINED", "FAILED", "CANCELED"} for s in studies.values()
        ),
        "live study in report",
    )
    history, oracle = studies["history"], studies["oracle"]
    require(history["state"] == oracle["state"] == "COMPLETED", "incomplete history/oracle")
    observed = [o for s in studies.values() for o in s["observations"]]
    ids = [o["attempt_id"] for o in observed]
    require(len(ids) == len(set(ids)), "study attempts overlap")
    row_ids = [r["attempt_id"] for r in rows]
    require(len(row_ids) == len(set(row_ids)) and set(row_ids) == set(ids), "accounting coverage")
    by_id = {r["attempt_id"]: r for r in rows}
    for study in studies.values():
        for obs in study["observations"]:
            row = by_id[obs["attempt_id"]]
            require(row["study_ref"] == study["ref"], "accounting study mismatch")
            require(
                seconds(row["allocated_device_seconds"]) == obs["device_seconds"], "cost mismatch"
            )
            require(row["mode"] == obs["mode"], "cost phase mismatch")
    source = {o["attempt_id"] for o in history["observations"] if o["mode"] == "confirmation"}
    require(len(source) == 9 and len(history["observations"]) == 15, "history cohort incomplete")
    history_wall = seconds(history["recommendation"]["cost"]["wall_seconds"])
    history_gpu = sum(
        by_id[o["attempt_id"]]["allocated_device_seconds"] for o in history["observations"]
    )
    baseline_spec = history["spec"]
    prior_cost = 0
    if "predecessor" in design:
        require(predecessor is not None, "retained predecessor report required")
        prior_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(predecessor, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        require(
            prior_digest == design["predecessor"]["stop_report_digest"]
            and predecessor["status"] == "stopped"
            and predecessor["trial_must_not_resume"],
            "predecessor evidence differs from frozen plan",
        )
        require(
            predecessor["results_ledger_api_s3_mlflow_verified"] == predecessor["application_jobs"],
            "predecessor accounting is incomplete",
        )
        prior_cost = seconds(predecessor["application_gpu_reservation_seconds"]) + seconds(
            predecessor["qualification_gpu_reservation_seconds"]
        )
        require(
            prior_cost == design["predecessor"]["retained_gpu_reservation_seconds"],
            "predecessor cost differs from frozen plan",
        )
    require(
        all(
            baseline_spec["identity"][key] == design["workload"][key]
            for key in ("input_shape", "seed", "work_units", "precision")
        ),
        "workload differs from frozen design",
    )
    resources = [c["context"]["resources"] for c in baseline_spec["candidates"]]
    require(
        sorted(r["host_cpu"] for r in resources) == sorted(design["candidate_cpu_cores"])
        and all(
            r["host_memory_mib"] == design["host_memory_mib"]
            and r["accelerator_count"] == design["gpu_count"]
            for r in resources
        )
        and baseline_spec["baseline_candidate_ref"] == design["baseline"],
        "resource space differs from frozen design",
    )
    for candidate in baseline_spec["candidates"]:
        require(
            sum(
                o["mode"] == "confirmation"
                and o["candidate_ref"] == candidate["ref"]
                and feasible(o, baseline_spec["quality"])
                for o in history["observations"]
            )
            == 3,
            "infeasible or unbalanced history confirmations",
        )
    require(oracle["spec"] == baseline_spec, "oracle workload changed")
    protocol_wall = (
        datetime.fromisoformat(oracle["recommendation"]["created_at"])
        - datetime.fromisoformat(report["protocol_started_at"])
    ).total_seconds()
    require(
        0 < protocol_wall <= caps["whole_protocol_wall_seconds"],
        "whole-protocol wall cap exceeded",
    )
    require(
        all(comparable(studies[label]["spec"]) == comparable(baseline_spec) for label in labels),
        "comparison workload/quality/resources changed",
    )
    sequence = [history, *(studies[label] for label in labels), oracle]
    for study in sequence:
        require(
            all(
                datetime.fromisoformat(study["created_at"])
                <= datetime.fromisoformat(o["recorded_at"])
                for o in study["observations"]
            ),
            "study contains observations from before its creation",
        )
    for left, right in zip(sequence, sequence[1:], strict=False):
        boundary = datetime.fromisoformat(right["created_at"])
        require(datetime.fromisoformat(left["created_at"]) < boundary, "study order changed")
        require(
            all(datetime.fromisoformat(o["recorded_at"]) < boundary for o in left["observations"]),
            "study observations overlap next study",
        )
        if left.get("recommendation"):
            require(
                datetime.fromisoformat(left["recommendation"]["created_at"]) < boundary,
                "selection overlaps next study",
            )
    digests = set()
    for slot, label in zip(schedule, labels, strict=True):
        study = studies[label]
        require(
            all(study["request"][k] == slot[k] for k in ("strategy", "seed")),
            "seed/strategy changed",
        )
        policy = study["spec"]["profiling"]
        lookup = slot["strategy"] == "lookup"
        expected_wall = (
            math.floor(caps["per_study_wall_seconds"] - history_wall)
            if lookup
            else caps["per_study_wall_seconds"]
        )
        expected_gpu = (
            caps["per_study_device_seconds"] - history_gpu
            if lookup
            else caps["per_study_device_seconds"]
        )
        require(policy["total_wall_seconds"] == expected_wall, "first-use wall budget changed")
        require(
            len(policy["device_seconds"]) == 1
            and next(iter(policy["device_seconds"].values())) == expected_gpu,
            "first-use GPU budget changed",
        )
        require(
            policy["max_probes"] == caps["per_study_max_probes"]
            and policy["final_validation_seconds"] == caps["final_validation_reserved_seconds"],
            "probe/reserve budget changed",
        )
        spent = sum(
            by_id[o["attempt_id"]]["allocated_device_seconds"] for o in study["observations"]
        )
        require(spent <= expected_gpu, "GPU cap exceeded")
        if study.get("recommendation"):
            require(
                seconds(study["recommendation"]["cost"]["wall_seconds"]) <= expected_wall,
                "wall cap exceeded",
            )
        if lookup:
            require(
                set(study["request"]["lookup_profile_refs"]) == source, "explicit history changed"
            )
            cohort = study["lookup_history"]
            require(set(cohort["profile_refs"]) == source, "future/oracle history leakage")
            historical = report["lookup_recommendations"][label]
            require(
                historical["ref"] == study["lookup_recommendation_ref"],
                "lookup recommendation mismatch",
            )
            require(
                historical["lookup_history"]["cohort_digest"] == cohort["cohort_digest"],
                "history digest changed",
            )
            require(
                set(historical["lookup_history"]["profile_refs"]) == source,
                "ranker history changed",
            )
            require(
                all(set(rank["evidence_refs"]) <= source for rank in historical["ranking"]),
                "ranking contains future evidence",
            )
            digests.add(cohort["cohort_digest"])
        else:
            require(
                not study.get("lookup_history") and not study["request"].get("lookup_profile_refs"),
                "cold-start strategy used history",
            )
        pilots = [o for o in study["observations"] if o["mode"] == "pilot"]
        require(
            len(pilots) <= (0 if lookup else caps["per_study_max_probes"]), "probe count exceeded"
        )
        rec = study.get("recommendation")
        if rec:
            confirmed = [o for o in study["observations"] if o["mode"] == "confirmation"]
            require(
                set(rec["confirmation_run_ids"]) == {o["attempt_id"] for o in confirmed},
                "confirmation cohort mismatch",
            )
            for candidate in {rec["predicted_candidate"], baseline_spec["baseline_candidate_ref"]}:
                require(
                    sum(
                        o["candidate_ref"] == candidate and feasible(o, baseline_spec["quality"])
                        for o in confirmed
                    )
                    >= baseline_spec["quality"]["minimum_repeats"],
                    "missing independent finalist/baseline confirmations",
                )
    require(len(digests) == 1, "lookup history differs between blocks")
    plans = report["plans"]
    expected_plans = [p for s in studies.values() for p in s["plans"]]
    require(
        len(plans) == len(expected_plans) and {p["ref"] for p in plans} == set(expected_plans),
        "plan coverage",
    )
    by_study = {s["ref"]: s for s in studies.values()}
    bo_updates = []
    for p in plans:
        s = by_study[p["study_ref"]]
        require(p["ref"] in s["plans"], "plan study mismatch")
        choice = p["choice"]
        surrogate = choice.get("surrogate", {})
        training = set(surrogate.get("training_run_ids", []))
        allowed = {
            o["attempt_id"]
            for o in s["observations"]
            if o["mode"] == "pilot"
            and datetime.fromisoformat(o["recorded_at"]) < datetime.fromisoformat(p["created_at"])
        }
        require(training <= allowed, "surrogate contains future/confirmation/history evidence")
        require(
            not surrogate.get("source_run_ids")
            and not choice.get("source_run_ids")
            and not choice.get("warm_start"),
            "unexpected transfer evidence",
        )
        if training:
            bo_updates.append(
                {"study_ref": s["ref"], "plan_ref": p["ref"], "training_attempts": sorted(training)}
            )
    means = {}
    for c in baseline_spec["candidates"]:
        observations = [
            o
            for o in oracle["observations"]
            if o["mode"] == "confirmation" and o["candidate_ref"] == c["ref"]
        ]
        require(len(observations) == 3, "oracle confirmation count")
        for o in observations:
            require(
                feasible(o, baseline_spec["quality"]),
                "infeasible oracle",
            )
        means[c["ref"]] = statistics.mean(
            o["measurements"]["elapsed_seconds"] for o in observations
        )
    best = min(means.values())
    summaries = []
    for slot, label in zip(schedule, labels, strict=True):
        study = studies[label]
        records = [r for r in rows if r["study_ref"] == study["ref"]]
        rec = study.get("recommendation")
        chosen = rec["confirmed_candidate"] if rec else None
        wall = rec["cost"]["wall_seconds"] if rec else None
        gpu = sum(r["allocated_device_seconds"] for r in records)
        lookup = slot["strategy"] == "lookup"
        summaries.append(
            {
                **slot,
                "label": label,
                "state": study["state"],
                "selected_candidate": chosen,
                "recommendation_status": rec["status"] if rec else None,
                "stop_reason": study.get("stop_reason"),
                "posthoc_selection_regret_fraction": means[chosen] / best - 1 if chosen else None,
                "independent_confirmation_mean_seconds": next(
                    r["mean_seconds"] for r in rec["ranking"] if r["candidate_ref"] == chosen
                )
                if rec
                else None,
                "study_wall_seconds": wall,
                "planning_seconds": study["planning_seconds"],
                "allocated_gpu_seconds": gpu,
                "first_use_gpu_seconds_including_history": gpu + (history_gpu if lookup else 0),
                "first_use_wall_seconds_including_history": wall + (history_wall if lookup else 0)
                if wall is not None
                else None,
                "pilot_jobs": sum(r["mode"] == "pilot" for r in records),
                "confirmation_jobs": sum(r["mode"] == "confirmation" for r in records),
                "failed_jobs": sum(r["outcome"] != "COMPLETED" for r in records),
                "model_based_choices": sum(
                    p["study_ref"] == study["ref"]
                    and p["choice"].get("reason") == "CONSTRAINED_QLOGNEI"
                    for p in plans
                ),
                "choice_reasons": [
                    p["choice"].get("reason") for p in plans if p["study_ref"] == study["ref"]
                ],
                "queue_seconds_known": sum(
                    seconds(r["queue_seconds"]) for r in records if r["queue_seconds"] is not None
                ),
                "unknown_queue_intervals": sum(r["queue_seconds"] is None for r in records),
            }
        )
    paired = []
    for left, right in itertools.combinations(["lookup", "random", "qlognei"], 2):
        for block in range(3):
            a = next(s for s in summaries if s["block"] == block and s["strategy"] == left)
            b = next(s for s in summaries if s["block"] == block and s["strategy"] == right)
            paired.append(
                {
                    "block": block,
                    "left": left,
                    "right": right,
                    "first_use_gpu_seconds_difference": a["first_use_gpu_seconds_including_history"]
                    - b["first_use_gpu_seconds_including_history"],
                    "regret_fraction_difference": a["posthoc_selection_regret_fraction"]
                    - b["posthoc_selection_regret_fraction"]
                    if a["selected_candidate"] and b["selected_candidate"]
                    else None,
                }
            )
    f0 = report["separate_qualification"]
    require(len(f0) == design["maximum_jobs"]["qualification"], "F0 coverage")
    require(len(rows) + len(f0) <= design["maximum_jobs"]["total"], "protocol job cap")
    trial_gpu = sum(r["allocated_device_seconds"] for r in rows) + sum(
        seconds(q["gpu_reservation_seconds"]) for q in f0
    )
    return {
        "scope": "descriptive three-block single-GPU comparison; no powered superiority claim",
        "whole_protocol_wall_seconds": protocol_wall,
        "studies": summaries,
        "block_paired_differences": paired,
        "oracle_confirmation_mean_seconds": means,
        "history_cost": {
            "jobs": 15,
            "profile_jobs": 9,
            "wall_seconds": history_wall,
            "gpu_seconds": history_gpu,
        },
        "three_block_gpu_cost_including_history_once": {
            strategy: sum(
                s["allocated_gpu_seconds"] for s in summaries if s["strategy"] == strategy
            )
            + (history_gpu if strategy == "lookup" else 0)
            for strategy in ["lookup", "random", "qlognei"]
        },
        "oracle_gpu_seconds": sum(
            r["allocated_device_seconds"] for r in rows if r["study_ref"] == oracle["ref"]
        ),
        "qualification_gpu_seconds": sum(seconds(q["gpu_reservation_seconds"]) for q in f0),
        "total_gpu_reservation_seconds": trial_gpu,
        "retained_predecessor_gpu_reservation_seconds": prior_cost,
        "cumulative_project_gpu_reservation_seconds": trial_gpu + prior_cost,
        "bo_updates": bo_updates,
        "frozen_history_and_temporal_leakage_audit_passed": True,
        "limitations": [
            "One numerical CNN fixture and one GPU; no trained-model accuracy or multi-backend effectiveness claim.",
            "S0 has prior observations; their full acquisition cost is included. S1/S2 start cold.",
            "Three temporal blocks are not independent devices or workload families.",
            "Oracle estimates use later independent runs, not known ground truth; nonstationarity and finite-sample uncertainty remain.",
            "GPU reservation time is not active compute time or measured energy; unknown queue durations stay unknown.",
            "No production break-even or statistical superiority inference.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--predecessor-stop",
        type=Path,
        default=Path(__file__).parents[1] / "docs/evidence/policy-stop.json",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path(__file__).parents[1] / "docs/evidence/policy-comparison-plan.json",
    )
    args = parser.parse_args()
    capture = json.loads(args.capture.read_text())
    require(
        capture["plan"] == json.loads(args.plan.read_text()), "capture differs from frozen plan"
    )
    predecessor = (
        json.loads(args.predecessor_stop.read_text()) if "predecessor" in capture["plan"] else None
    )
    result = summarize(capture, predecessor=predecessor)
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write("\n")
