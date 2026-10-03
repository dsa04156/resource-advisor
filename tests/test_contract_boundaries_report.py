import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture
def auditor(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "examples"))
    spec = importlib.util.spec_from_file_location(
        "audit_contract_boundaries", ROOT / "examples/audit_contract_boundaries.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("contract_boundary_cases", None)


@pytest.fixture
def report():
    return json.loads((ROOT / "docs/evidence/contract-boundaries-v1.json").read_text())


def test_live_boundary_evidence_rechecks_without_mutation(auditor, report):
    original = deepcopy(report)
    summary = auditor.audit(report)
    assert summary["scope_cases"] == 5
    assert summary["budget_cases"] == 4
    assert summary["control_profiles"] == 12
    assert summary["qualification_gpu_seconds"] == 2
    assert summary["api_jobs_created"] == 0
    assert report == original


@pytest.mark.parametrize(
    "fault",
    [
        "execution",
        "profile",
        "qualification",
        "cost",
        "accepted",
        "cause",
        "request",
        "registration",
        "case",
        "future",
        "control",
        "thermal",
    ],
)
def test_invalid_boundary_evidence_is_rejected(auditor, report, fault):
    if fault == "execution":
        report["preservation"]["api_jobs_created"] = 1
    elif fault == "profile":
        report["preservation"]["f0_results_in_profiles"] = 1
    elif fault == "qualification":
        report["qualification"]["result"]["evidence_kind"] = "synthetic"
    elif fault == "cost":
        report["qualification"]["gpu_reservation_seconds"] = 0
    elif fault == "accepted":
        report["responses"]["shape-submission"]["status_code"] = 200
    elif fault == "cause":
        report["responses"]["shape-submission"]["response"]["detail"] = "CAPABILITY_STALE"
    elif fault == "request":
        report["responses"]["shape-submission"]["request"]["workload_ref"] = "wrong-workload"
    elif fault == "registration":
        report["responses"]["fresh-workload"]["response"]["profiling"]["consent"] = True
    elif fault == "case":
        report["scope_cases"].pop()
    elif fault == "future":
        report["control_support_profiles"][0]["recorded_at"] = "2099-01-01T00:00:00+00:00"
    elif fault == "control":
        report["control_support_profiles"].pop()
    else:
        report["qualification"]["thermal_trace"]["windows"][0]["before"]["temperature_c"] = 100
    with pytest.raises(ValueError):
        auditor.audit(report)
