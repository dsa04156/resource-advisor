import json
import platform

import pytest

from resource_advisor import cpu_benchmark as cpu
from resource_advisor.contracts import signature


def environment(monkeypatch):
    monkeypatch.setattr(cpu, "cpu_model", lambda: "fixture CPU")
    monkeypatch.setattr(cpu.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cpu.platform, "machine", lambda: "x86_64")
    values = {
        "RA_CONTEXT_JSON": json.dumps(
            {
                "arch": "amd64",
                "allocation_mode": "cpu_only",
                "memory_model": "host",
                "accelerator_model": "fixture CPU",
                "resources": {"accelerator_count": 0},
                "runtime_versions": {"python": platform.python_version()},
            }
        ),
        "RA_INPUT_SHAPE": "[16, 16]",
        "RA_WORK_UNITS": "3",
        "RA_PRECISION": "fp64",
        "RA_SEED": "0",
        "RA_JOB_ID": "cpu-test",
        "RA_ATTEMPT_ID": "cpu-test-attempt",
        "RA_EPOCH": "1",
        "RA_WORKLOAD_SIGNATURE": signature("workload"),
        "RA_CONTEXT_SIGNATURE": signature("context"),
        "NVIDIA_VISIBLE_DEVICES": "void",
        "CUDA_VISIBLE_DEVICES": "",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_cpu_computation_checks_every_output_and_result_identity(monkeypatch, capsys):
    environment(monkeypatch)
    result = cpu.run()
    assert result.measurements.quality_value == 1
    assert result.measurements.work_units == 3
    assert result.measurements.elapsed_seconds > 0
    assert result.measurements.peak_memory_mib > 0
    message = json.loads(capsys.readouterr().out.split("RESOURCE_ADVISOR_RESULT ")[1])
    assert message["digest"] == signature(result)
    output = cpu.multiply(16)
    output[7][11] += 1
    assert cpu.numerical_agreement(output, 16) == 255 / 256


@pytest.mark.parametrize(
    "key,value",
    [
        ("NVIDIA_VISIBLE_DEVICES", "all"),
        ("CUDA_VISIBLE_DEVICES", "0"),
        ("RA_PRECISION", "fp32"),
        ("RA_WORK_UNITS", "21"),
        ("RA_INPUT_SHAPE", "[129, 129]"),
        ("RA_SEED", "1"),
    ],
)
def test_cpu_fixture_refuses_unqualified_inputs(monkeypatch, key, value):
    environment(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises((ValueError, RuntimeError)):
        cpu.run()
