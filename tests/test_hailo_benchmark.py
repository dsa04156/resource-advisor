import json
from pathlib import Path

import pytest

from resource_advisor.backends import result_from_log
from resource_advisor.contracts import ExecutionResult, QualityPolicy, signature
from resource_advisor.hailo_benchmark import (
    BOUNDARY,
    HEF,
    MANIFEST,
    MODEL,
    PRECISION,
    Binding,
    run,
)
from resource_advisor.hailo_qualification import quality_gates


@pytest.fixture
def case():
    report = json.loads(
        (Path(__file__).parents[1] / "docs/evidence/hailo-resnet50-qualification.json").read_text()
    )
    quality = QualityPolicy(minimum=0.75, maximum_peak_memory_mib=1024)
    binding = Binding.model_validate(
        {
            "identity": {
                "code_digest": signature("isolated-test-runner"),
                "model_digest": MODEL,
                "dataset_version": "disjoint100:" + MANIFEST,
                "config_digest": signature({"hef": HEF, "manifest": MANIFEST}),
                "task_type": "inference",
                "input_shape": [1, 224, 224, 3],
                "batch_size": 1,
                "precision": PRECISION,
                "seed": 20261005,
                "work_units": 100,
                "quality_contract_digest": signature(quality),
                "measurement_boundary": BOUNDARY,
            },
            "context": {
                "arch": "arm64",
                "environment_digest": signature("isolated-test-environment"),
                "runtime_versions": {
                    k: report[k] for k in ["hailort", "driver", "firmware", "python", "numpy"]
                },
                "accelerator_model": "HAILO8",
                "memory_model": "discrete",
                "power_mode": "not-measured",
                "allocation_mode": "physical_device",
                "resources": {"host_cpu": 1, "host_memory_mib": 1024, "accelerator_count": 1},
            },
            "hef_digest": HEF,
            "manifest_digest": MANIFEST,
        }
    )
    env = {
        "RA_JOB_ID": "test-job",
        "RA_ATTEMPT_ID": "test-attempt",
        "RA_EPOCH": "1",
        "RA_WORKLOAD_SIGNATURE": signature(binding.identity),
        "RA_CONTEXT_SIGNATURE": signature("test-context"),
        "RA_CONTEXT_JSON": binding.context.model_dump_json(),
        "RA_INPUT_SHAPE": "[1,224,224,3]",
        "RA_WORK_UNITS": "100",
        "RA_PRECISION": PRECISION,
        "RA_SEED": "20261005",
        "RA_EXECUTION_MODE": "observe",
    }
    return binding, env, report


def test_qualified_report_becomes_one_bound_result(case, capsys):
    binding, env, report = case
    result = run(binding, Path("fixture"), Path("model.hef"), env=env, execute=lambda *a: report)
    envelope = result_from_log(capsys.readouterr().out)
    assert ExecutionResult.model_validate(envelope["result"]) == result
    assert envelope["digest"] == signature(result)
    assert result.outcome == "COMPLETED"
    assert result.measurements.quality_value == 0.8
    assert result.measurements.peak_memory_mib == report["host_process_peak_rss_mib"]
    assert result.measurements.gpu_utilization is None
    assert result.measurements.power_watts is None
    assert result.measurements.work_units == result.measurements.sample_count == 100


@pytest.mark.parametrize("gate", ["accuracy", "agreement", "loss"])
def test_every_quality_failure_retains_failed_result_without_timings(case, gate, capsys):
    binding, env, report = case
    # Controlled gate failures, not additional hardware measurements.
    for i, row in enumerate(report["predictions"]):
        row.update(label=0, prediction=0 if i < 80 else 1, reference_top1=0 if i < 80 else 1)
        if gate == "accuracy":
            row.update(prediction=1, reference_top1=1)
        elif gate == "agreement" and i >= 80:
            row["reference_top1"] = 2
        elif gate == "loss":
            row["reference_top1"] = 0
    rows = report["predictions"]
    report["quality"] = quality_gates(
        [r["prediction"] for r in rows],
        [r["label"] for r in rows],
        [r["reference_top1"] for r in rows],
    )
    result = run(binding, Path("fixture"), Path("model.hef"), env=env, execute=lambda *a: report)
    assert result.outcome == "FAILED" and result.measurements is None
    assert result.error_code == "HAILO_QUALITY_GATE_FAILED"
    assert result_from_log(capsys.readouterr().out)["digest"] == signature(result)


@pytest.mark.parametrize(
    "key,value",
    [
        ("RA_INPUT_SHAPE", "[1,3,224,224]"),
        ("RA_WORK_UNITS", "99"),
        ("RA_PRECISION", "fp32"),
        ("RA_SEED", "1"),
        ("RA_WORKLOAD_SIGNATURE", signature("other")),
        ("RA_EXECUTION_MODE", "pilot"),
        ("RA_EPOCH", "0"),
    ],
)
def test_contract_mismatch_prevents_device_execution(case, key, value):
    binding, env, _ = case
    env[key] = value

    def forbidden(*args):
        pytest.fail("device must not be acquired for an unqualified contract")

    with pytest.raises(ValueError):
        run(binding, Path("fixture"), Path("model.hef"), env=env, execute=forbidden)


@pytest.mark.parametrize("kind", ["context", "model", "manifest", "runtime", "quality"])
def test_bound_context_and_actual_evidence_are_enforced(case, kind):
    binding, env, report = case
    if kind == "context":
        context = json.loads(env["RA_CONTEXT_JSON"])
        context["resources"]["accelerator_count"] = 2
        env["RA_CONTEXT_JSON"] = json.dumps(context)
    elif kind == "model":
        value = binding.model_dump(mode="json")
        value["identity"]["model_digest"] = signature("wrong-model")
        binding = Binding.model_validate(value)
    elif kind == "manifest":
        report["manifest_sha256"] = "0" * 64
    elif kind == "runtime":
        report["driver"] = "4.22.0"
    else:
        report["quality"]["accuracy"] = 1
    with pytest.raises(ValueError):
        run(binding, Path("fixture"), Path("model.hef"), env=env, execute=lambda *a: report)
