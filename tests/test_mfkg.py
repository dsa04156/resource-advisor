import importlib.util
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from resource_advisor.mfkg import MFKernelInput, ask_mfkg


@pytest.fixture
def problem():
    path = Path(__file__).resolve().parents[1] / "examples/mfkg_numerical_demo.py"
    spec = importlib.util.spec_from_file_location("mfkg_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fixture()


def changed(problem, **updates):
    return MFKernelInput.model_validate({**problem.model_dump(mode="json"), **updates})


def test_actual_mfkg_matches_finite_fantasy_value_and_target_projection(problem):
    torch = pytest.importorskip("torch")
    pytest.importorskip("botorch")
    before = torch.get_num_threads()
    result = ask_mfkg(problem)
    assert torch.get_num_threads() == before
    assert result["surrogate"]["class"] == "SingleTaskMultiFidelityGP"
    assert len(result["surrogate"]["training_run_ids"]) == 18
    targets = {o.ref for o in problem.options if o.fidelity == 1}
    for row in result["scores"]:
        cost = row["normalized_cost"]
        expected = (
            sum(g / cost if g > 0 else g * cost for g in row["fantasy_gains"])
            / problem.num_fantasies
        )
        assert row["acquisition_value"] == pytest.approx(expected, abs=1e-8)
        assert set(row["fantasy_target_options"]) <= targets
        assert len(row["fantasy_target_options"]) == problem.num_fantasies
    assert result["option_ref"] in {o.ref for o in problem.options}
    selected = next(o for o in problem.options if o.ref == result["option_ref"])
    assert (result["candidate_ref"], result["fidelity"]) == (
        selected.candidate_ref,
        selected.fidelity,
    )
    assert all(p["fidelity"] == 1 and p["measured"] is False for p in result["predictions"])
    assert result["execution_authorized"] is False
    assert result["planning_seconds"] >= result["fit_seconds"] > 0


def test_cost_changes_acquisition_without_changing_gp_observations(problem):
    pytest.importorskip("botorch")
    original = ask_mfkg(problem)
    low = next(o.ref for o in problem.options if o.fidelity < 1)
    records = [r.model_dump() for r in problem.observations]
    for row in records:
        if row["option_ref"] == low:
            row["evaluation_wall_seconds"] *= 100
    expensive = ask_mfkg(changed(problem, observations=records))
    a = next(r for r in original["scores"] if r["option_ref"] == low)
    b = next(r for r in expensive["scores"] if r["option_ref"] == low)
    assert b["acquisition_value"] < a["acquisition_value"]
    assert b["fantasy_gains"] == pytest.approx(a["fantasy_gains"], abs=1e-9)
    assert original["predictions"] == expensive["predictions"]


def test_unit_rescaling_of_wall_time_does_not_change_selection(problem):
    pytest.importorskip("botorch")
    records = [r.model_dump() for r in problem.observations]
    for row in records:
        row["evaluation_wall_seconds"] *= 60
    a, b = ask_mfkg(problem), ask_mfkg(changed(problem, observations=records))
    assert a["option_ref"] == b["option_ref"]
    assert [r["acquisition_value"] for r in a["scores"]] == pytest.approx(
        [r["acquisition_value"] for r in b["scores"]], abs=1e-9
    )


def test_expensive_low_fidelity_can_select_target_level(problem):
    pytest.importorskip("botorch")
    low = {o.ref for o in problem.options if o.fidelity < 1}
    records = [r.model_dump() for r in problem.observations]
    for row in records:
        if row["option_ref"] in low:
            row["evaluation_wall_seconds"] *= 1000
    result = ask_mfkg(changed(problem, observations=records))
    assert result["fidelity"] == 1.0
    assert result["option_ref"] not in low


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "missing_target",
        "unknown_option",
        "bad_cost",
        "mixed_dimensions",
        "mixed_runtime",
        "failed_quality",
        "repeat_only",
    ],
)
def test_invalid_or_unpaired_inputs_are_rejected(problem, case):
    data = problem.model_dump(mode="json")
    if case == "duplicate":
        data["observations"][1]["attempt_id"] = data["observations"][0]["attempt_id"]
    elif case == "missing_target":
        data["options"][1]["fidelity"] = 0.8
    elif case == "unknown_option":
        data["observations"][0]["option_ref"] = "not-approved"
    elif case == "bad_cost":
        data["observations"][0]["evaluation_wall_seconds"] = 0
    elif case == "mixed_dimensions":
        data["options"][0]["coordinates"] = [0, 1]
    elif case == "mixed_runtime":
        data["observations"][0]["runtime_group_signature"] = "sha256:" + "a" * 64
    elif case == "failed_quality":
        data["observations"][0]["quality_passed"] = False
    else:
        data["fidelity_axis"] = "identical_input_repetition"
    with pytest.raises(ValidationError):
        MFKernelInput.model_validate(data)


def test_fit_failure_is_not_a_fake_random_mfkg_suggestion(problem, monkeypatch):
    botorch_fit = pytest.importorskip("botorch.fit")
    import torch

    def fail(*args, **kwargs):
        raise RuntimeError("controlled fit failure")

    monkeypatch.setattr(botorch_fit, "fit_gpytorch_mll", fail)
    before = torch.get_num_threads()
    with pytest.raises(RuntimeError, match="controlled fit failure"):
        ask_mfkg(problem)
    assert torch.get_num_threads() == before


def test_analysis_cli_does_not_open_database_or_submit_jobs(problem, monkeypatch, tmp_path, capsys):
    pytest.importorskip("botorch")
    from resource_advisor import cli, configuration

    def forbidden(*args, **kwargs):
        pytest.fail("numerical analysis must not open database or backend")

    monkeypatch.setattr(cli, "Store", forbidden)
    monkeypatch.setattr(configuration, "KubernetesBackend", forbidden)
    monkeypatch.setattr(configuration, "SlurmBackend", forbidden)
    source, output = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text(problem.model_dump_json())
    monkeypatch.setattr(
        "sys.argv",
        ["resource-advisor", "mfkg-analyze", "--input", str(source), "--output", str(output)],
    )
    cli.main()
    import json

    result = json.loads(output.read_text())
    assert result["execution_authorized"] is False
    assert math.isfinite(result["current_target_value"])
    assert '"execution_authorized": false' in capsys.readouterr().out
