"""Synthetic mathematical checks. These are not hardware transfer evidence."""

import json

import pytest
from pydantic import ValidationError

from resource_advisor.contracts import signature
from resource_advisor.rgpe import RGPEInput, ask_rgpe, history_order


@pytest.fixture
def problem():
    runtime = signature("test-runtime")

    def task(name, elapsed):
        return {
            "workload_signature": signature(name),
            "runtime_group_signature": runtime,
            "observations": [
                {
                    "attempt_id": f"{name}-{i}-{repeat}",
                    "candidate_ref": f"config-{i}",
                    "elapsed_seconds": seconds * (1 + repeat * 0.005),
                    "evaluation_wall_seconds": 2 + seconds,
                    "peak_memory_mib": 100 + i * 5 + repeat,
                    "quality_value": 0.99 - repeat * 0.001,
                    "quality_passed": True,
                    "memory_passed": True,
                }
                for i, seconds in enumerate(elapsed)
                for repeat in range(2)
            ],
        }

    return RGPEInput.model_validate(
        {
            "runtime_group_signature": runtime,
            "options": [{"candidate_ref": f"config-{i}", "coordinates": [i / 3]} for i in range(4)],
            "feature_names": ["host_cpu"],
            "sources": [task("source", [50, 20, 8, 2])],
            "target": task("target", [12, 6, 3, 1]),
            "minimum_quality": 0.9,
            "maximum_peak_memory_mib": 200,
            "seed": 11,
        }
    )


def changed(problem, mutate):
    data = problem.model_dump(mode="json")
    mutate(data)
    return RGPEInput.model_validate(data)


def test_actual_gp_ensemble_uses_target_loo_and_separate_source_evidence(problem):
    torch = pytest.importorskip("torch")
    pytest.importorskip("botorch")
    previous = torch.get_num_threads()
    result = ask_rgpe(problem)
    assert torch.get_num_threads() == previous
    assert result["method"] == "rank_weighted_gp_ensemble_qLogNEI"
    assert sum(result["weights"].values()) == pytest.approx(1)
    assert result["weights"][problem.sources[0].workload_signature] > 0
    assert result["candidate_ref"] in {o.candidate_ref for o in problem.options}
    assert result["execution_authorized"] is False
    assert all(p["measured"] is False for p in result["predictions"])
    target_ids = {r.attempt_id for r in problem.target.observations}
    source_ids = {r.attempt_id for r in problem.sources[0].observations}
    surrogate = result["surrogate"]
    assert set(surrogate["training_run_ids"]) == target_ids
    assert set(surrogate["source_run_ids"]) == source_ids
    for fold in surrogate["target_loo"]:
        held_out, training = set(fold["held_out_run_ids"]), set(fold["training_run_ids"])
        assert len(held_out) == 2
        assert held_out.isdisjoint(training | source_ids)
        assert held_out | training == target_ids
    assert result["historical_source_wall_seconds"] > result["new_target_wall_seconds"]
    json.dumps(result, allow_nan=False)


def test_absolute_source_time_rescaling_never_rescales_target_predictions(problem):
    pytest.importorskip("botorch")

    def rescale(data):
        for row in data["sources"][0]["observations"]:
            row["elapsed_seconds"] *= 1000

    a, b = ask_rgpe(problem), ask_rgpe(changed(problem, rescale))
    assert a["candidate_ref"] == b["candidate_ref"]
    assert a["weights"] == pytest.approx(b["weights"])
    for pa, pb in zip(a["predictions"], b["predictions"], strict=True):
        assert pa["predicted_elapsed_seconds"] == pytest.approx(
            pb["predicted_elapsed_seconds"], rel=1e-5
        )
        assert pa["predicted_memory_mib"] == pb["predicted_memory_mib"]
        assert pa["predicted_quality"] == pb["predicted_quality"]


def test_reversed_source_ranks_zero_weight_and_target_only_fallback(problem):
    pytest.importorskip("botorch")

    def reverse(data):
        for row in data["sources"][0]["observations"]:
            row["elapsed_seconds"] = 1 / row["elapsed_seconds"]

    result = ask_rgpe(changed(problem, reverse))
    assert result["weights"]["target"] == 1
    assert result["weights"][problem.sources[0].workload_signature] == 0
    assert result["rank_diagnostics"]["source_mean_losses"][0] > 0.9
    assert result["reason"] == "TARGET_ONLY_QLOGNEI_FALLBACK"


def test_indistinguishable_target_ranks_disable_transfer(problem):
    pytest.importorskip("botorch")

    def tie(data):
        for row in data["target"]["observations"]:
            row["elapsed_seconds"] = 2.0

    result = ask_rgpe(changed(problem, tie))
    assert result["weights"]["target"] == 1
    assert result["fallback_reason"] == "NO_INFORMATIVE_TARGET_RANKS"


def test_initial_target_checks_are_balanced_and_source_independent(problem):
    pytest.importorskip("botorch")

    def few(data):
        data["target"]["observations"] = data["target"]["observations"][:2]

    initial = changed(problem, few)
    result = ask_rgpe(initial)
    assert result["reason"] == "TARGET_CHECKS_REQUIRED"
    assert result["candidate_ref"] == "config-1"
    assert result["weights"] is None
    warm = history_order(initial)
    assert warm["method"] == "history_guided_warm_start"
    assert warm["candidate_order"][0] == "config-3"
    assert "weights" not in warm


@pytest.mark.parametrize(
    "case",
    [
        "reused_attempt",
        "target_as_source",
        "unknown_config",
        "mixed_runtime",
        "unreplicated_source",
        "duplicate_coordinates",
        "failed_quality",
        "false_flag",
    ],
)
def test_invalid_transfer_inputs_rejected(problem, case):
    data = problem.model_dump(mode="json")
    if case == "reused_attempt":
        data["target"]["observations"][0]["attempt_id"] = data["sources"][0]["observations"][0][
            "attempt_id"
        ]
    elif case == "target_as_source":
        data["sources"][0]["workload_signature"] = data["target"]["workload_signature"]
    elif case == "unknown_config":
        data["target"]["observations"][0]["candidate_ref"] = "unknown"
    elif case == "mixed_runtime":
        data["sources"][0]["runtime_group_signature"] = signature("different")
    elif case == "unreplicated_source":
        data["sources"][0]["observations"].pop()
    elif case == "duplicate_coordinates":
        data["options"][1]["coordinates"] = data["options"][0]["coordinates"]
    elif case == "failed_quality":
        data["target"]["observations"][0]["quality_value"] = 0.1
    else:
        data["target"]["observations"][0]["quality_passed"] = False
    with pytest.raises(ValidationError):
        RGPEInput.model_validate(data)


def test_failure_restores_threads_and_never_invents_rgpe_suggestion(problem, monkeypatch):
    fitting = pytest.importorskip("botorch.fit")
    import torch

    def fail(*args, **kwargs):
        raise RuntimeError("controlled fit failure")

    monkeypatch.setattr(fitting, "fit_gpytorch_mll", fail)
    previous = torch.get_num_threads()
    with pytest.raises(RuntimeError, match="controlled fit failure"):
        ask_rgpe(problem)
    assert torch.get_num_threads() == previous
