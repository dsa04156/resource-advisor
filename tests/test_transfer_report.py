import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

path = Path(__file__).parents[1] / "examples/evaluate_transfer.py"
spec = importlib.util.spec_from_file_location("evaluate_transfer", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def report():
    """Synthetic audit fixture, not experimental evidence."""
    target_spec = {
        "candidates": [{"ref": "a"}, {"ref": "b"}],
        "profiling": {
            "max_probes": 8,
            "total_wall_seconds": 1200,
            "final_validation_seconds": 360,
            "device_seconds": {"gpu": 900},
        },
        "quality": {"minimum_repeats": 3, "minimum": 1, "maximum_peak_memory_mib": 2048},
    }
    result = {
        "plan": {
            "sources": [128, 192],
            "target_schedule": [
                {"block": 0, "strategy": "rgpe", "seed": 42},
                {"block": 0, "strategy": "random", "seed": 42},
            ],
            "per_target_study": {
                "max_probes": 8,
                "total_wall_seconds": 1200,
                "final_validation_seconds": 360,
                "device_seconds": 900,
                "minimum_confirmation_repeats": 3,
            },
        },
        "studies": {},
        "plans": [],
        "observations": [],
        "source_evidence": {
            "provenance": [],
            "recorded_at": "2026-01-01T03:00:00+00:00",
            "historical_source_wall_seconds": 12,
        },
    }
    for hour, label in enumerate(
        ["source-0", "source-1", "target-0-rgpe", "target-0-random", "oracle"], 1
    ):
        # Sources end before the 03:00 source snapshot; targets start after it.
        hour = hour if hour < 3 else hour + 1
        study = {
            "ref": label,
            "state": "COMPLETED",
            "spec": deepcopy(target_spec),
            "request": {"strategy": label.split("-")[-1], "seed": 42},
            "created_at": f"2026-01-01T{hour:02}:00:00+00:00",
            "planning_seconds": 0.5,
            "observations": [],
            "plans": [],
            "recommendation": {
                "status": "PRESERVE_BASELINE_UNCERTAINTY",
                "confirmed_candidate": "a",
                "ranking": [{"candidate_ref": "a", "mean_seconds": 2}],
                "cost": {"wall_seconds": 10},
            },
        }
        for i in range(7):
            attempt = f"{label}-attempt-{i}"
            plan_ref = f"{label}-plan-{i}"
            obs = {
                "attempt_id": attempt,
                "candidate_ref": "a" if i < 4 else "b",
                "mode": "pilot" if i == 0 else "confirmation",
                "outcome": "COMPLETED",
                "recorded_at": f"2026-01-01T{hour:02}:00:{2 * i + 1:02}+00:00",
                "measurements": {
                    "elapsed_seconds": 2 if i < 4 else 1,
                    "quality_value": 1,
                    "peak_memory_mib": 5,
                },
            }
            study["observations"].append(obs)
            study["plans"].append(plan_ref)
            result["plans"].append(
                {
                    "ref": plan_ref,
                    "study_ref": label,
                    "choice": {},
                    "created_at": f"2026-01-01T{hour:02}:00:{2 * i:02}+00:00",
                }
            )
            result["observations"].append(
                {
                    "attempt_id": attempt,
                    "study_ref": label,
                    "allocated_device_seconds": 2,
                    "queue_seconds": 0,
                }
            )
            if label.startswith("source") and i > 0:
                result["source_evidence"]["provenance"].append({"attempt_id": attempt})
        result["studies"][label] = study
    plan = next(p for p in result["plans"] if p["ref"] == "target-0-rgpe-plan-1")
    plan["choice"] = {
        "method": "rank_weighted_gp_ensemble_qLogNEI",
        "surrogate": {
            "training_run_ids": ["target-0-rgpe-attempt-0"],
            "source_run_ids": [p["attempt_id"] for p in result["source_evidence"]["provenance"]],
        },
        "weights": {"target": 1, "source-0": 0, "source-1": 0},
        "rank_diagnostics": {"fixture": True},
        "fallback_reason": "TARGET_ONLY",
    }
    return result


def test_descriptive_costs_and_posthoc_regret(report):
    output = module.summarize(report)
    assert len(output["target_studies"]) == 2
    assert output["target_studies"][0]["posthoc_selection_regret_fraction"] == 1
    assert output["target_studies"][0]["allocated_gpu_seconds"] == 14
    assert output["source_characterization_cost"]["gpu_reservation_seconds"] == 28
    assert output["source_characterization_cost"]["selected_profile_jobs"] == 12
    assert output["oracle_gpu_reservation_seconds"] == 14
    assert output["rgpe_updates"][0]["weights"]["target"] == 1


@pytest.mark.parametrize(
    "leak", ["source-0-attempt-1", "target-0-rgpe-attempt-1", "oracle-attempt-0"]
)
def test_rejects_source_confirmation_or_oracle_as_target_training(report, leak):
    plan = next(p for p in report["plans"] if p["choice"].get("surrogate"))
    plan["choice"]["surrogate"]["training_run_ids"].append(leak)
    with pytest.raises(ValueError, match="unauthorized source evidence"):
        module.summarize(report)


@pytest.mark.parametrize("cost", [None, -1, float("nan"), float("inf")])
@pytest.mark.parametrize("label", ["source-0", "target-0-rgpe", "oracle"])
def test_rejects_invalid_allocation_cost_for_every_phase(report, cost, label):
    next(r for r in report["observations"] if r["study_ref"] == label)[
        "allocated_device_seconds"
    ] = cost
    with pytest.raises(ValueError, match="GPU allocation"):
        module.summarize(report)


def test_rejects_cherry_picked_sources(report):
    report["source_evidence"]["provenance"].pop()
    with pytest.raises(ValueError, match="entire preregistered"):
        module.summarize(report)


def test_rejects_missing_optimizer_plan(report):
    report["plans"].pop()
    with pytest.raises(ValueError, match="plans do not cover"):
        module.summarize(report)


def test_rejects_consistently_increased_budgets(report):
    for study in report["studies"].values():
        study["spec"]["profiling"]["max_probes"] = 20
    with pytest.raises(ValueError, match="budgets differ from the frozen"):
        module.summarize(report)


def test_rejects_wrong_accounting_owner(report):
    report["observations"][0]["study_ref"] = "oracle"
    with pytest.raises(ValueError, match="wrong study"):
        module.summarize(report)


def test_rejects_infeasible_oracle(report):
    report["studies"]["oracle"]["observations"][1]["measurements"]["quality_value"] = 0.5
    with pytest.raises(ValueError, match="not feasible"):
        module.summarize(report)


def test_rejects_live_study_and_early_oracle(report):
    report["studies"]["target-0-rgpe"]["state"] = "EXPLORING"
    with pytest.raises(ValueError, match="live study"):
        module.summarize(report)
    report["studies"]["target-0-rgpe"]["state"] = "COMPLETED"
    report["studies"]["oracle"]["created_at"] = "2026-01-01T04:00:00+00:00"
    with pytest.raises(ValueError, match="overlap target"):
        module.summarize(report)
