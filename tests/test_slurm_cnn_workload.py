import copy
import importlib.util
import json
from pathlib import Path

import pytest

from resource_advisor.backends import result_from_log
from resource_advisor.contracts import ExecutionResult, signature

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("slurm_cnn", ROOT / "examples/slurm_cnn_workload.py")
workload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workload)


def environment():
    return {
        "RA_JOB_ID": "job-fixture",
        "RA_ATTEMPT_ID": "attempt-fixture",
        "RA_EPOCH": "1",
        "RA_WORKLOAD_SIGNATURE": "sha256:" + "a" * 64,
        "RA_CONTEXT_SIGNATURE": "sha256:" + "b" * 64,
        "RA_CONTEXT_JSON": json.dumps(
            {
                "arch": "arm64",
                "accelerator_model": "Orin",
                "memory_model": "unified",
                "allocation_mode": "physical_device",
                "runtime_versions": workload.VERSIONS,
                "resources": {"host_cpu": 1, "host_memory_mib": 1024, "accelerator_count": 1},
                "parameters": {},
            }
        ),
        "RA_INPUT_SHAPE": "[4,3,32,32]",
        "RA_PRECISION": "fp32",
        "RA_SEED": "20261006",
        "RA_WORK_UNITS": "10",
        "RA_EXECUTION_MODE": "observe",
    }


def measurement():
    # Historical hardware values test serialization, not a new GPU execution.
    return json.loads((ROOT / "docs/evidence/slurm-orin-torch-v2.json").read_text())["measurement"]


def test_native_producer_round_trips_through_real_platform_contract():
    identity = workload.request_identity(environment(), "aarch64")
    measured = measurement()
    value = workload.envelope(identity, measured)
    parsed = result_from_log("other output\nRESOURCE_ADVISOR_RESULT " + json.dumps(value))
    result = ExecutionResult.model_validate(parsed["result"])
    assert parsed["digest"] == signature(result)
    assert result.job_id == identity["job_id"] and result.epoch == 1
    assert result.measurements.work_units == 10
    assert result.measurements.throughput == 10 / sum(measured["samples_seconds"])
    assert result.measurements.peak_memory_mib == measured["peak_tensor_allocation_mib"]
    assert result.measurements.power_watts is None
    assert result.measurements.gpu_utilization is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("RA_WORK_UNITS", "9"),
        ("RA_PRECISION", "fp16"),
        ("RA_SEED", "42"),
        ("RA_INPUT_SHAPE", "[1,3,32,32]"),
        ("RA_EXECUTION_MODE", "pilot"),
        ("RA_SAMPLING_PLAN_JSON", "{}"),
        ("RA_THERMAL_POLICY_JSON", "{}"),
        ("RA_JOB_ID", "../other"),
        ("RA_EPOCH", "0"),
        ("RA_CONTEXT_SIGNATURE", "unsigned"),
    ],
)
def test_changed_execution_or_identity_is_rejected_before_torch(key, value):
    env = environment()
    env[key] = value
    with pytest.raises(ValueError):
        workload.request_identity(env, "aarch64")


@pytest.mark.parametrize(
    "field,value",
    [
        ("resources", {"host_cpu": 2, "host_memory_mib": 1024, "accelerator_count": 1}),
        ("memory_model", "discrete"),
        ("accelerator_model", "different"),
        ("runtime_versions", {**workload.VERSIONS, "cuda": "13.0"}),
        ("allocation_mode", "virtual_slot"),
        ("parameters", {"input_strategy": "cache"}),
    ],
)
def test_unqualified_context_rejected(field, value):
    env = environment()
    context = json.loads(env["RA_CONTEXT_JSON"])
    context[field] = value
    env["RA_CONTEXT_JSON"] = json.dumps(context)
    with pytest.raises(ValueError, match="fixed native CNN"):
        workload.request_identity(env, "aarch64")
    with pytest.raises(ValueError):
        workload.request_identity(environment(), "x86_64")


@pytest.mark.parametrize("change", ["failure", "partial", "nan", "empty"])
def test_failure_or_incomplete_samples_cannot_become_completed(change):
    measured = copy.deepcopy(measurement())
    if change == "failure":
        measured["result"] = "FAIL"
    elif change == "partial":
        measured["compared_elements"] = 360
    elif change == "nan":
        measured["samples_seconds"][0] = float("nan")
    else:
        measured["samples_seconds"] = []
    with pytest.raises(ValueError, match="complete measured"):
        workload.envelope(workload.request_identity(environment(), "aarch64"), measured)
