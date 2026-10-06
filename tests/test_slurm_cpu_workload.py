import importlib.util
import json
from pathlib import Path

import pytest

from resource_advisor.backends import result_from_log
from resource_advisor.contracts import ExecutionResult, signature

spec = importlib.util.spec_from_file_location(
    "native_cpu", Path(__file__).parents[1] / "examples/slurm_cpu_workload.py"
)
cpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpu)
HOST = {
    "arch": "arm64",
    "accelerator_model": "fixture board / 0x41:0xd03",
    "runtime_versions": {"python": "3.10.12"},
}


def environment():
    return {
        "RA_JOB_ID": "cpu-fixture",
        "RA_ATTEMPT_ID": "cpu-attempt",
        "RA_EPOCH": "1",
        "RA_WORKLOAD_SIGNATURE": signature("workload"),
        "RA_CONTEXT_SIGNATURE": signature("context"),
        "RA_CONTEXT_JSON": json.dumps(
            {
                **HOST,
                "memory_model": "host",
                "allocation_mode": "cpu_only",
                "resources": {"host_cpu": 1, "host_memory_mib": 1024, "accelerator_count": 0},
                "parameters": {},
            }
        ),
        "RA_INPUT_SHAPE": "[64,64]",
        "RA_WORK_UNITS": "5",
        "RA_PRECISION": "fp64",
        "RA_SEED": "0",
        "RA_EXECUTION_MODE": "observe",
        "SLURM_JOB_ID": "123",
        "SLURM_CPUS_PER_TASK": "1",
    }


def test_native_cpu_result_round_trips_through_platform_contract():
    # Local numerical/serialization test with synthetic host/Slurm identities.
    # This is not evidence of a real ARM node or Slurm allocation.
    envelope = cpu.run(environment(), HOST)
    parsed = result_from_log("RESOURCE_ADVISOR_RESULT " + json.dumps(envelope))
    result = ExecutionResult.model_validate(parsed["result"])
    assert parsed["digest"] == signature(result)
    assert result.measurements.quality_value == 1
    assert result.measurements.work_units == result.measurements.sample_count == 5
    assert result.measurements.gpu_utilization is None
    output = cpu.compute()
    output[7][11] += 1
    with pytest.raises(ValueError, match="numerical agreement"):
        cpu.validate(output)


@pytest.mark.parametrize(
    "key,value",
    [
        ("SLURM_JOB_ID", ""),
        ("SLURM_CPUS_PER_TASK", "2"),
        ("SLURM_JOB_GPUS", "0"),
        ("CUDA_VISIBLE_DEVICES", "0"),
        ("RA_WORK_UNITS", "6"),
        ("RA_PRECISION", "fp32"),
        ("RA_EPOCH", "0"),
        ("RA_JOB_ID", "../other"),
        ("RA_THERMAL_POLICY_JSON", "{}"),
    ],
)
def test_unqualified_execution_rejected_before_compute(monkeypatch, key, value):
    monkeypatch.setattr(cpu, "compute", lambda: pytest.fail("must reject before computation"))
    env = environment()
    env[key] = value
    with pytest.raises(ValueError):
        cpu.run(env, HOST)


@pytest.mark.parametrize(
    "field,value",
    [
        ("arch", "amd64"),
        ("accelerator_model", "different"),
        ("runtime_versions", {"python": "3.12.0"}),
        ("resources", {"host_cpu": 1, "host_memory_mib": 1024, "accelerator_count": 1}),
        ("allocation_mode", "physical_device"),
        ("parameters", {"batch": 2}),
    ],
)
def test_changed_host_or_context_rejected(field, value):
    env = environment()
    context = json.loads(env["RA_CONTEXT_JSON"])
    context[field] = value
    env["RA_CONTEXT_JSON"] = json.dumps(context)
    with pytest.raises(ValueError):
        cpu.request_identity(env, HOST)
