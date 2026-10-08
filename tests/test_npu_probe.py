"""Software contract checks only; these do not qualify a hardware device."""

import pytest

from resource_advisor.npu_probe import npu_execution_devices


def test_direct_npu_device_property_accepts_string_and_vector_representations():
    assert npu_execution_devices("NPU") == ["NPU"]
    assert npu_execution_devices(["NPU.0"]) == ["NPU.0"]
    for value in [[], "CPU", ["NPU", "CPU"], "NPU,CPU", "NPU-fake", None]:
        with pytest.raises(ValueError, match="exclusively NPU"):
            npu_execution_devices(value)


def test_nearest_rank_percentiles_and_canonical_envelope(monkeypatch, capsys):
    from resource_advisor import npu_probe
    from resource_advisor.contracts import ExecutionResult, signature

    identity = {
        "input_shape": [1, 3, 16, 16],
        "work_units": 10,
        "precision": "fixed",
        "seed": npu_probe.SEED,
        "measurement_boundary": npu_probe.BOUNDARY,
        "model_digest": "sha256:" + "a" * 64,
        "code_digest": "sha256:" + "b" * 64,
        "dataset_version": "generated-fixed-input:sha256:" + "c" * 64,
    }
    context = {
        "resources": {"host_cpu": 1, "host_memory_mib": 1024, "accelerator_count": 1},
        "parameters": {},
        "allocation_mode": "physical_device",
        "accelerator_model": "test-NPU",
        "runtime_versions": {"synthetic-test-only": "1"},
    }
    binding = {
        "identity": identity,
        "context": context,
        "model_digest": identity["model_digest"],
        "accelerator_model": "test-NPU",
        "runtime": "intel-conv",
        "minimum_quality": 1,
    }
    import json

    env = {
        "RA_WORKLOAD_SIGNATURE": npu_probe.digest(identity),
        "RA_CONTEXT_JSON": json.dumps(context),
        "RA_INPUT_SHAPE": json.dumps(identity["input_shape"]),
        "RA_WORK_UNITS": "10",
        "RA_PRECISION": "fixed",
        "RA_SEED": str(npu_probe.SEED),
        "RA_EXECUTION_MODE": "observe",
        "RA_CONTEXT_SIGNATURE": "sha256:" + "d" * 64,
        "RA_JOB_ID": "j-synthetic",
        "RA_ATTEMPT_ID": "a-synthetic",
        "RA_EPOCH": "1",
    }
    # Synthetic producer data exercises serialization, never a hardware experiment.
    report = {
        **identity,
        "accelerator_model": "test-NPU",
        "runtime_versions": context["runtime_versions"],
        "quality_value": 1,
        "samples_seconds": [i / 10 for i in range(1, 11)],
        "elapsed_seconds": 5.5,
        "host_process_peak_rss_mib": 10,
        "runner_digest": identity["code_digest"],
        "input_digest": "sha256:" + "c" * 64,
    }
    monkeypatch.setattr(npu_probe, "qualify", lambda runtime: report)
    result = npu_probe.execute(binding, env)
    envelope = json.loads(capsys.readouterr().out.split("RESOURCE_ADVISOR_RESULT ")[1])
    assert result["measurements"]["latency_p95_ms"] == 1000
    assert result["measurements"]["latency_p99_ms"] == 1000
    assert envelope["digest"] == signature(ExecutionResult.model_validate(result))


def test_qualification_rejects_forged_numerical_success():
    import importlib.util
    from pathlib import Path

    import pytest

    path = Path(__file__).parents[1] / "examples/build_npu_target.py"
    spec = importlib.util.spec_from_file_location("build_npu_target", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from resource_advisor.npu_probe import BOUNDARY, ROUNDS, SEED, digest

    report = {
        "runtime": "intel-conv",
        "evidence_kind": "hardware",
        "kind": "npu-runtime-qualification",
        "arch": "amd64",
        "input_shape": [1, 3, 16, 16],
        "work_units": ROUNDS,
        "seed": SEED,
        "warmup": 1,
        "measurement_boundary": BOUNDARY,
        "samples_seconds": [1.0] * ROUNDS,
        "elapsed_seconds": 10.0,
        "quality_value": 1,
        "host_process_peak_rss_mib": 10,
        "model_spec": {"synthetic": True},
        "execution_devices": ["NPU"],
        "maximum_absolute_errors": [0.0] * ROUNDS,
        "outputs": [[[99.0] * 4]] * ROUNDS,
        "reference_output": [[0.0] * 4],
    }
    report["model_digest"] = digest(report["model_spec"])
    with pytest.raises(ValueError, match="numerical reference"):
        module.build(
            report,
            ref="synthetic",
            project="test",
            node="test",
            cluster="test",
            image="example.invalid/image@sha256:" + "a" * 64,
            evidence_ref="synthetic-software-fixture",
            observed_at="2000-01-01T00:00:00+00:00",
        )


def test_unknown_firmware_allows_manual_observe_but_blocks_recommendation(bundle):
    from resource_advisor.policy import compatibility, execution_compatibility

    spec, candidate, variant, cap = bundle
    versions = {"profile_reuse": "blocked-unobserved-host-firmware"}
    context = candidate.context.model_copy(update={"runtime_versions": versions})
    candidate = candidate.model_copy(update={"context": context})
    variant = variant.model_copy(update={"runtime_versions": versions})
    cap = cap.model_copy(update={"runtime_versions": versions})
    assert "RUNTIME_FINGERPRINT_INCOMPLETE" in compatibility(spec, candidate, variant, cap)
    assert execution_compatibility(spec, candidate, variant, cap, operational=True) == []


@pytest.mark.parametrize("failure", ["construction", "release"])
def test_proxy_cleanup_even_when_sdk_initialization_or_release_fails(failure):
    import subprocess
    import sys

    from resource_advisor.npu_probe import rknn_session

    processes = []

    def launch(command):
        process = subprocess.Popen(command)
        processes.append(process)
        return process

    class SDK:
        def release(self):
            if failure == "release":
                raise RuntimeError("SDK release failure")

    def construct():
        if failure == "construction":
            raise RuntimeError("SDK construction failure")
        return SDK()

    with pytest.raises(RuntimeError, match="SDK"):
        with rknn_session(
            [sys.executable, "-c", "import time; time.sleep(30)"], construct, launch=launch
        ):
            pass
    assert processes[0].poll() is not None


def test_legacy_npu_without_firmware_fingerprint_cannot_reuse_profile(bundle):
    from resource_advisor.policy import compatibility, execution_compatibility

    spec, candidate, variant, cap = bundle
    variant = variant.model_copy(update={"accelerator_vendor": "intel", "device_class": "npu"})
    cap = cap.model_copy(update={"accelerator_vendor": "intel", "device_class": "npu"})
    assert "RUNTIME_FINGERPRINT_INCOMPLETE" in compatibility(spec, candidate, variant, cap)
    assert execution_compatibility(spec, candidate, variant, cap, operational=True) == []
