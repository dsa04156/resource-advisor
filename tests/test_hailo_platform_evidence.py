import importlib.util
import json
from pathlib import Path

import pytest

from resource_advisor.contracts import signature

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "audit_hailo_platform", ROOT / "examples/audit_hailo_platform.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def evidence():
    return (
        json.loads((ROOT / "docs/evidence/hailo-resnet50-platform.json").read_text()),
        json.loads((ROOT / "docs/evidence/hailo-resnet50-inputs.json").read_text()),
    )


def test_actual_protocol_has_four_delivered_jobs_and_retains_prediction_limit(evidence):
    result = module.audit(*evidence)
    assert result["verified_api_jobs"] == 4
    assert result["qualification_jobs"] == 1
    assert result["independent_accuracy_images"] == 100
    assert result["api_npu_reservation_seconds"] == 12
    assert result["qualification_npu_reservation_seconds"] == 8
    assert result["later_verification_inside_mean_interval"] is False


@pytest.mark.parametrize(
    "fault,match",
    [
        ("replacement", "exactly three"),
        ("allocation", "allocation duration"),
        ("duplicate", "duplicate MLflow"),
        ("delivery", "live delivery"),
        ("reference", "reference changed"),
        ("future", "future verification"),
    ],
)
def test_audit_rejects_incomplete_or_misattributed_evidence(evidence, fault, match):
    data, manifest = evidence
    first = data["runs"][0]
    if fault == "replacement":
        data["runs"].append(first)
    elif fault == "allocation":
        first["ledger"]["allocated_device_seconds"] = first["report"]["elapsed_seconds"]
    elif fault == "duplicate":
        data["runs"][1]["delivery"]["mlflow_run_id"] = first["delivery"]["mlflow_run_id"]
    elif fault == "delivery":
        first["delivery"]["matching_s3_api_mlflow_bytes"] = False
    elif fault == "reference":
        first["report"]["predictions"][0]["reference_top1"] = 999
        first["report_digest"] = signature(first["report"])
    else:
        rec = data["recommendation"]
        rec["ranking"][0]["evidence_refs"][0] = data["runs"][3]["result"]["attempt_id"]
        rec["digest"] = signature({k: v for k, v in rec.items() if k != "digest"})
        data["approval"]["recommendation_digest"] = rec["digest"]
    with pytest.raises(ValueError, match=match):
        module.audit(data, manifest)
