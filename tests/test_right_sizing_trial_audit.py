"""Mutate captured evidence to test auditor rejection, without running benchmarks."""

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from audit_right_sizing_trial import (
    audit,
    audit_hailo_feedback,
    audit_phase_readback,
    compare_reference,
)  # noqa: E402


@pytest.fixture
def evidence():
    root = Path(__file__).resolve().parents[1] / "docs/evidence"
    return (
        json.loads((root / "right-sizing-hailo-v1.json").read_text()),
        json.loads((root / "hailo-resnet50-inputs.json").read_text()),
    )


def test_captured_hailo_feedback_recomputes(evidence):
    report, manifest = evidence
    audited = audit_hailo_feedback(report, manifest)
    assert audited["feedback_status"] == "COMPARABLE"
    assert (
        audited["total_npu_reservation_seconds"]
        == sum(r["ledger"]["allocated_device_seconds"] for r in report["runs"])
        + report["qualification"]["scheduled_to_container_finished_seconds"]
    )


@pytest.mark.parametrize("fault", ["digest", "cost", "duplicate", "future", "residual", "reuse"])
def test_hailo_auditor_rejects_corrupted_capture(evidence, fault):
    original, manifest = evidence
    report = copy.deepcopy(original)
    if fault == "digest":
        report["runs"][0]["result"]["context_signature"] = "sha256:" + "0" * 64
    elif fault == "cost":
        report["runs"][0]["ledger"]["allocated_device_seconds"] = None
    elif fault == "duplicate":
        report["runs"][1]["backend"]["uid"] = report["runs"][0]["backend"]["uid"]
    elif fault == "future":
        report["runs"][0]["profile"]["recorded_at"] = "2099-01-01T00:00:00+00:00"
    elif fault == "residual":
        report["runs"][3]["feedback"]["residual_seconds"] += 1
    else:
        report["runs"][3]["feedback"]["source_profile_refs"].append(
            report["runs"][3]["result"]["attempt_id"]
        )
    with pytest.raises(ValueError):
        audit_hailo_feedback(report, manifest)


@pytest.fixture
def gpu_evidence():
    root = Path(__file__).resolve().parents[1] / "docs/evidence"
    return (
        json.loads((root / "right-sizing-gpu-v1.json").read_text()),
        json.loads((root / "right-sizing-trial-plan-v1.json").read_text()),
    )


def test_gpu_capture_includes_failed_reference_cost_and_finite_n(gpu_evidence):
    report, plan = gpu_evidence
    checked = audit(report, plan)
    failed = [a for a in report["attempts"] if a["state"] != "SUCCEEDED"]
    assert failed and checked["failures"] == len(failed)
    assert all(a["device_seconds"] > 0 for a in failed)
    assert checked["total_device_seconds"] == sum(
        a["device_seconds"] for a in report["attempts"]
    ) + sum(q["device_seconds"] for q in report["qualifications"])
    assert all(
        p["N"] <= plan["gpu_search"]["main_reuses"] for p in checked["cumulative_measured_cost"]
    )
    assert all(not p["reference_complete"] for p in checked["search_comparison"])


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "cost",
        "digest",
        "quality",
        "future_model",
        "confirmation",
        "planning",
        "budget",
        "stale",
        "observation",
        "censored",
    ],
)
def test_gpu_auditor_rejects_corrupted_capture(gpu_evidence, fault):
    original, plan = gpu_evidence
    report = copy.deepcopy(original)
    entry = next(s for s in report["studies"] if s["strategy"] == "qlognei")
    if fault == "duplicate":
        report["attempts"].append(copy.deepcopy(report["attempts"][0]))
    elif fault == "cost":
        report["attempts"][0]["device_seconds"] = None
    elif fault == "digest":
        row = next(a for a in report["attempts"] if a["result"])
        row["result"]["context_signature"] = "sha256:" + "0" * 64
    elif fault == "quality":
        report["attempts"][0]["spec"]["quality"]["minimum"] = -1
    elif fault == "future_model":
        probe = next(p for p in entry["plans"] if p["body"]["choice"].get("surrogate"))
        probe["body"]["created_at"] = "2000-01-01T00:00:00+00:00"
    elif fault == "confirmation":
        probe = next(o for o in entry["study"]["observations"] if o["mode"] == "pilot")
        entry["study"]["recommendation"]["ranking"][0]["evidence_refs"].append(probe["attempt_id"])
    elif fault == "planning":
        entry["plans"][0]["body"]["choice"]["planning_seconds"] = None
    elif fault == "budget":
        unit = next(iter(entry["study"]["charged_device_seconds"]))
        entry["study"]["charged_device_seconds"][unit] = (
            entry["study"]["spec"]["profiling"]["device_seconds"][unit] + 1
        )
    elif fault == "observation":
        entry["study"]["observations"][0]["measurements"]["elapsed_seconds"] *= 0.1
    elif fault == "censored":
        failed = next(a for a in report["attempts"] if a["state"] != "SUCCEEDED")
        observation = next(
            o
            for s in report["studies"]
            for o in s["study"]["observations"]
            if o["attempt_id"] == failed["attempt_id"]
        )
        observation["outcome"] = "COMPLETED"
    else:
        report["stale_reuse"][0]["submission_http_status"] = 202
    with pytest.raises(ValueError):
        audit(report, plan)


@pytest.mark.parametrize("fault", [None, "identity", "digest", "overlap", "thermal"])
def test_phase_supplement_rejects_unattributed_or_invalid_data(gpu_evidence, fault):
    report, _ = gpu_evidence
    root = Path(__file__).resolve().parents[1] / "docs/evidence"
    raw = (root / "right-sizing-gpu-v1.json").read_bytes()
    supplement = json.loads((root / "right-sizing-phase-v1.json").read_text())
    if fault == "identity":
        supplement["runs"][1]["attempt_id"] = supplement["runs"][0]["attempt_id"]
    elif fault == "digest":
        supplement["primary_sha256"] = "0" * 64
    elif fault == "overlap":
        supplement["runs"][0]["phase"]["samples"][0]["phases_seconds"]["cpu_processing"] = 100
    elif fault == "thermal":
        supplement["runs"][0]["thermal_assessment"]["status"] = "unknown"
    if fault:
        with pytest.raises(ValueError):
            audit_phase_readback(supplement, report, hashlib.sha256(raw).hexdigest())
    else:
        checked = audit_phase_readback(supplement, report, hashlib.sha256(raw).hexdigest())
        assert checked["runs"] == len(supplement["runs"])


@pytest.mark.parametrize("fault", [None, "parent", "chronology", "runtime", "context", "reuse"])
def test_later_reference_cannot_change_primary_selection_or_leak(gpu_evidence, fault):
    report, plan = gpu_evidence
    root = Path(__file__).resolve().parents[1] / "docs/evidence"
    later = json.loads((root / "right-sizing-reference-recovery-v2.json").read_text())
    later_plan = json.loads((root / "right-sizing-reference-recovery-plan-v2.json").read_text())
    if fault == "parent":
        later["parent_experiment_id"] = "other"
    elif fault == "chronology":
        later_plan["declared_at"] = "2000-01-01T00:00:00+00:00"
    elif fault == "runtime":
        later["attempts"][0]["variant"]["command"] = ["python", "other.py"]
    elif fault == "context":
        later["studies"][0]["study"]["spec"]["candidates"][0]["context"]["power_mode"] = "changed"
    elif fault == "reuse":
        later["attempts"][0]["native"]["job_uid"] = report["attempts"][0]["native"]["job_uid"]
    if fault:
        with pytest.raises(ValueError):
            compare_reference(report, plan, later, later_plan)
    else:
        result = compare_reference(report, plan, later, later_plan)
        assert all(c["reference_complete"] for c in result["comparison"] if c["workload"] == "W1")
        assert all(
            c["relative_distance_to_measured_reference"] is None
            for c in result["comparison"]
            if c["workload"] == "W2"
        )
        assert result["failures_including_original_reference"] > 0


def test_declared_partial_reference_does_not_invent_other_workload(gpu_evidence):
    report, plan = gpu_evidence
    root = Path(__file__).resolve().parents[1] / "docs/evidence"
    later = json.loads((root / "right-sizing-reference-recovery-v2.json").read_text())
    later_plan = json.loads((root / "right-sizing-reference-recovery-plan-v2.json").read_text())
    result = compare_reference(report, plan, later, later_plan, workloads=("W2",))
    assert {c["workload"] for c in result["comparison"]} == {"W2"}
    assert all(c["relative_distance_to_measured_reference"] is None for c in result["comparison"])
    with pytest.raises(ValueError, match="subset"):
        compare_reference(report, plan, later, later_plan, workloads=("W2", "W2"))
