import hashlib
import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

path = Path(__file__).parents[1] / "examples/evaluate_policy_comparison.py"
spec = importlib.util.spec_from_file_location("evaluate_policy_comparison", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def report():
    """Synthetic temporal and accounting fixture; not hardware evidence."""
    schedule = [
        {"order": 1 + block * 3 + i, "block": block, "strategy": strategy, "seed": 42 + block}
        for block in range(3)
        for i, strategy in enumerate(["lookup", "random", "qlognei"])
    ]
    baseline = {
        "ref": "workload",
        "identity": {
            "model": "fixture",
            "input_shape": [1, 3, 256, 256],
            "seed": 42,
            "work_units": 12,
            "precision": "fp32",
        },
        "baseline_candidate_ref": "a",
        "candidates": [
            {
                "ref": c,
                "variant_ref": "v-" + c,
                "context": {
                    "resources": {
                        "host_cpu": i + 1,
                        "host_memory_mib": 2048,
                        "accelerator_count": 1,
                    }
                },
            }
            for i, c in enumerate("abc")
        ],
        "quality": {"minimum_repeats": 3, "minimum": 1, "maximum_peak_memory_mib": 2048},
        "profiling": {
            "max_probes": 8,
            "total_wall_seconds": 1200,
            "final_validation_seconds": 360,
            "device_seconds": {"gpu": 900},
        },
    }
    result = {
        "phase": "completed",
        "protocol_started_at": "2026-01-01T00:00:00+00:00",
        "studies": {},
        "plans": [],
        "observations": [],
        "lookup_recommendations": {},
        "separate_qualification": [{"gpu_reservation_seconds": 2} for _ in range(3)],
        "plan": {
            "workload": {
                "input_shape": [1, 3, 256, 256],
                "seed": 42,
                "work_units": 12,
                "precision": "fp32",
            },
            "candidate_cpu_cores": [1, 2, 3],
            "host_memory_mib": 2048,
            "gpu_count": 1,
            "baseline": "a",
            "schedule": schedule,
            "maximum_jobs": {"qualification": 3, "total": 135},
            "budgets": {
                "per_study_max_probes": 8,
                "per_study_wall_seconds": 1200,
                "per_study_device_seconds": 900,
                "final_validation_reserved_seconds": 360,
                "whole_protocol_wall_seconds": 7200,
            },
        },
    }
    source = []
    entries = [
        ("history", "grid_characterization", 42),
        *[(f"target-{s['block']}-{s['strategy']}", s["strategy"], s["seed"]) for s in schedule],
        ("oracle", "grid_characterization", 142),
    ]
    for order, (label, strategy, seed) in enumerate(entries):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=order * 2)

        def stamp(seconds, start=start):
            return (start + timedelta(seconds=seconds)).isoformat()

        study = {
            "ref": label,
            "state": "COMPLETED",
            "spec": deepcopy(baseline),
            "request": {"strategy": strategy, "seed": seed},
            "created_at": stamp(0),
            "planning_seconds": 0.2,
            "plans": [],
            "observations": [],
        }
        if strategy == "lookup":
            study["spec"]["ref"] = "lookup"
            study["spec"]["profiling"].update(total_wall_seconds=1100, device_seconds={"gpu": 885})
            study["request"]["lookup_profile_refs"] = source[:]
            study["lookup_history"] = {"profile_refs": source[:], "cohort_digest": "frozen"}
            study["lookup_recommendation_ref"] = "rank-" + label
            result["lookup_recommendations"][label] = {
                "ref": study["lookup_recommendation_ref"],
                "lookup_history": deepcopy(study["lookup_history"]),
                "ranking": [{"evidence_refs": source[:]}],
            }
        pilots = 6 if strategy == "grid_characterization" else 0 if strategy == "lookup" else 3
        confirmations = list("aaabbbccc") if strategy == "grid_characterization" else list("aaabbb")
        sequence = [("pilot", "abc"[i % 3]) for i in range(pilots)] + [
            ("confirmation", c) for c in confirmations
        ]
        for i, (mode, candidate) in enumerate(sequence):
            attempt, plan_ref = f"{label}-attempt-{i}", f"{label}-plan-{i}"
            obs = {
                "attempt_id": attempt,
                "candidate_ref": candidate,
                "mode": mode,
                "outcome": "COMPLETED",
                "device_seconds": 1,
                "recorded_at": stamp(i * 2 + 2),
                "measurements": {
                    "quality_value": 1,
                    "peak_memory_mib": 5,
                    "elapsed_seconds": {"a": 2, "b": 1, "c": 3}[candidate],
                },
            }
            study["observations"].append(obs)
            study["plans"].append(plan_ref)
            choice = (
                {"surrogate": {"training_run_ids": [f"{label}-attempt-0"]}}
                if strategy == "qlognei" and i == 1
                else {}
            )
            result["plans"].append(
                {
                    "ref": plan_ref,
                    "study_ref": label,
                    "created_at": stamp(i * 2 + 1),
                    "choice": choice,
                }
            )
            result["observations"].append(
                {
                    **obs,
                    "study_ref": label,
                    "allocated_device_seconds": 1,
                    "queue_seconds": None if i == 0 else 0,
                }
            )
            if label == "history" and mode == "confirmation":
                source.append(attempt)
        study["recommendation"] = {
            "status": "PRESERVE_BASELINE_UNCERTAINTY",
            "confirmed_candidate": "a",
            "predicted_candidate": "b",
            "ranking": [{"candidate_ref": "a", "mean_seconds": 2}],
            "confirmation_run_ids": [
                o["attempt_id"] for o in study["observations"] if o["mode"] == "confirmation"
            ],
            "created_at": stamp(100),
            "cost": {"wall_seconds": 100},
        }
        result["studies"][label] = study
    return result


def test_history_is_charged_on_first_use_and_once_across_actual_reuse(report):
    output = module.summarize(report)
    lookup = output["studies"][0]
    assert lookup["allocated_gpu_seconds"] == 6
    assert lookup["first_use_gpu_seconds_including_history"] == 21
    assert lookup["posthoc_selection_regret_fraction"] == 1
    assert lookup["unknown_queue_intervals"] == 1
    assert output["three_block_gpu_cost_including_history_once"] == {
        "lookup": 33,
        "random": 27,
        "qlognei": 27,
    }
    assert output["qualification_gpu_seconds"] == 6
    assert len(output["bo_updates"]) == 3
    assert len(output["block_paired_differences"]) == 9


@pytest.mark.parametrize("fault", ["cohort", "ranking", "surrogate"])
def test_rejects_oracle_leakage_through_every_selection_path(report, fault):
    leak = "oracle-attempt-0"
    if fault == "cohort":
        report["studies"]["target-0-lookup"]["lookup_history"]["profile_refs"].append(leak)
    elif fault == "ranking":
        report["lookup_recommendations"]["target-0-lookup"]["ranking"][0]["evidence_refs"].append(
            leak
        )
    else:
        next(p for p in report["plans"] if p["choice"].get("surrogate"))["choice"]["surrogate"][
            "training_run_ids"
        ].append(leak)
    with pytest.raises(ValueError, match="leakage|future"):
        module.summarize(report)


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf")])
def test_unknown_or_invalid_cost_is_never_zero(report, value):
    report["observations"][0]["allocated_device_seconds"] = value
    with pytest.raises(ValueError, match="unknown/invalid cost"):
        module.summarize(report)


def test_history_cost_cannot_be_omitted_from_lookup_budget(report):
    report["studies"]["target-0-lookup"]["spec"]["profiling"]["total_wall_seconds"] = 1200
    with pytest.raises(ValueError, match="first-use wall"):
        module.summarize(report)


@pytest.mark.parametrize("fault", ["quality", "deadline", "space"])
def test_rejects_quality_time_and_search_space_violations(report, fault):
    if fault == "quality":
        for o in report["studies"]["target-0-lookup"]["observations"]:
            o["measurements"]["quality_value"] = 0
    elif fault == "deadline":
        report["protocol_started_at"] = "2025-01-01T00:00:00+00:00"
    else:
        for study in report["studies"].values():
            study["spec"]["identity"]["work_units"] = 100
    with pytest.raises(ValueError, match="confirmations|wall cap|frozen design"):
        module.summarize(report)


def predecessor_report(report):
    prior = {
        "status": "stopped",
        "trial_must_not_resume": True,
        "results_ledger_api_s3_mlflow_verified": 48,
        "application_jobs": 48,
        "application_gpu_reservation_seconds": 132,
        "qualification_gpu_reservation_seconds": 8,
    }
    report["plan"]["predecessor"] = {
        "stop_report_digest": "sha256:"
        + hashlib.sha256(
            json.dumps(prior, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "retained_gpu_reservation_seconds": 140,
    }
    return prior


def test_separate_trial_retains_failed_predecessor_cost_without_double_charging_policies(report):
    prior = predecessor_report(report)
    output = module.summarize(report, predecessor=prior)
    assert output["retained_predecessor_gpu_reservation_seconds"] == 140
    assert (
        output["cumulative_policy_trials_gpu_reservation_seconds"]
        == output["total_gpu_reservation_seconds"] + 140
    )
    assert output["three_block_gpu_cost_including_history_once"] == {
        "lookup": 33,
        "random": 27,
        "qlognei": 27,
    }


@pytest.mark.parametrize("fault", ["absent", "modified", "old-observation"])
def test_prior_failure_cannot_be_hidden_or_reused_as_a_fresh_observation(report, fault):
    prior = predecessor_report(report)
    if fault == "absent":
        prior = None
    elif fault == "modified":
        prior["application_gpu_reservation_seconds"] = 0
    else:
        report["studies"]["history"]["observations"][0]["recorded_at"] = "2025-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="predecessor|before its creation"):
        module.summarize(report, predecessor=prior)


@pytest.mark.parametrize("fault", ["workload", "repeat", "overlap", "missing", "duplicate"])
def test_rejects_unfair_or_incomplete_comparisons(report, fault):
    study = report["studies"]["target-0-random"]
    if fault == "workload":
        study["spec"]["identity"]["model"] = "different"
    elif fault == "repeat":
        study["recommendation"]["confirmation_run_ids"].pop()
    elif fault == "overlap":
        study["created_at"] = report["studies"]["history"]["created_at"]
    elif fault == "missing":
        report["observations"].pop()
    else:
        report["observations"].append(report["observations"][0])
    with pytest.raises(ValueError):
        module.summarize(report)
