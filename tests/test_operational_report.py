"""Evidence audits: mutations are invalid fixtures, never new hardware evidence."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "evaluate_operational_comparison", ROOT / "examples/evaluate_operational_comparison.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def evidence():
    return (
        json.loads((ROOT / "docs/evidence/operational-comparison-v1.json").read_text()),
        json.loads((ROOT / "docs/evidence/operational-plan-v1.json").read_text()),
    )


def test_published_report_is_reproducible_and_does_not_mutate_evidence(evidence):
    report, plan = evidence
    before = deepcopy(report)
    summary = module.summarize(report, plan)
    assert summary == json.loads(
        (ROOT / "docs/evidence/operational-comparison-v1-summary.json").read_text()
    )
    assert report == before
    assert summary["main_jobs"] == sum(a["count"] for a in summary["arms"].values())
    assert len(summary["actual_serial_cumulative"]) == plan["blocks"] * 3


@pytest.mark.parametrize(
    "fault",
    [
        "incomplete",
        "plan",
        "missing_run",
        "duplicate",
        "schedule",
        "gpu",
        "cpu",
        "approval",
        "mode",
        "digest",
        "delivery",
        "future_training",
        "unknown_cost",
        "fallback",
    ],
)
def test_misattributed_or_incomplete_comparison_is_rejected(evidence, fault):
    report, plan = evidence
    row = report["main"][0]
    if fault == "incomplete":
        report["phase"] = "main"
    elif fault == "plan":
        report["plan"]["seed"] += 1
    elif fault == "missing_run":
        report["main"].pop()
    elif fault == "duplicate":
        row["attempt_id"] = report["main"][1]["attempt_id"]
    elif fault == "schedule":
        report["main"][0], report["main"][1] = report["main"][1], report["main"][0]
    elif fault == "gpu":
        row["gpu_reservation_seconds"] += 1
    elif fault == "cpu":
        row["cpu_core_reservation_seconds"] += 1
    elif fault == "approval":
        report["approval"]["candidate_ref"] = "cpu0-5"
    elif fault == "mode":
        row["mode"] = "fixed"
    elif fault == "digest":
        row["result"]["measurements"]["elapsed_seconds"] = 0.001
    elif fault == "delivery":
        row["delivery"]["s3_api_mlflow_bytes_match"] = False
    elif fault == "future_training":
        p = next(p for p in report["profiling"]["plans"] if p["choice"].get("surrogate"))
        p["choice"]["surrogate"]["training_run_ids"].append(row["attempt_id"])
    elif fault == "unknown_cost":
        report["profiling"]["observations"][0]["allocated_device_seconds"] = None
    else:
        report["profiling"]["fallback_choices"] += 1
    with pytest.raises(ValueError):
        module.summarize(report, plan)


def test_future_b2_setup_is_charged_once_to_every_actual_use_count(evidence):
    report, plan = evidence
    summary = module.summarize(report, plan)
    for arm in ("B0", "B1", "B2"):
        steps = [s for s in summary["actual_serial_cumulative"] if s["arm"] == arm]
        runs = [r for r in summary["main_timings"] if r["arm"] == arm]
        for i, step in enumerate(steps):
            setup = summary["B2_setup_wall_seconds"] if arm == "B2" else 0
            assert step["raw_wall_seconds_including_profile"] == setup + sum(
                r["raw_result_wall_seconds"] for r in runs[: i + 1]
            )
            endpoint = "raw_result_wall_seconds" if arm == "B0" else "delivery_wall_seconds"
            assert step["observed_completion_seconds_including_profile"] == setup + sum(
                r[endpoint] for r in runs[: i + 1]
            )
            if arm == "B0":
                assert step["platform_recorded_delivery_seconds_including_profile"] is None


@pytest.mark.parametrize("fault", ["thermal", "phase"])
def test_measurement_classifications_are_recomputed(evidence, fault):
    report, plan = evidence
    row = report["main"][0]
    if fault == "thermal":
        row["thermal_trace"]["windows"][0]["before"]["temperature_c"] = 100
    else:
        row["phase_profile"]["samples"][0]["wall_seconds"] *= 2
    with pytest.raises(ValueError):
        module.summarize(report, plan)


def test_only_contract_defaults_are_normalized(evidence):
    report, plan = evidence
    expected = module.summarize(report, plan)["main_timings"]
    report["workload"]["execution"]["priority"] = "normal"
    assert module.summarize(report, plan)["main_timings"] == expected
    report["workload"]["execution"]["priority"] = "high"
    with pytest.raises(ValueError, match="workload mismatch"):
        module.summarize(report, plan)


def test_qualification_jobs_cannot_be_recounted(evidence):
    report, plan = evidence
    report["qualification"][1]["job_ref"] = report["qualification"][0]["job_ref"]
    with pytest.raises(ValueError, match="qualification candidate coverage"):
        module.summarize(report, plan)
