import json

from resource_advisor import cuda_probe
from resource_advisor.contracts import ExecutionResult, signature


def test_probe_envelope_matches_existing_result_contract(monkeypatch, capsys, bundle):
    # Only serialization is mocked here; hardware reports are collected separately.
    _, candidate, _, _ = bundle
    for key, value in {
        "RA_CONTEXT_JSON": candidate.context.model_dump_json(),
        "RA_INPUT_SHAPE": "[4096]",
        "RA_WORK_UNITS": "10",
        "RA_PRECISION": "int32",
        "RA_JOB_ID": "job-test",
        "RA_ATTEMPT_ID": "attempt-test",
        "RA_EPOCH": "1",
        "RA_WORKLOAD_SIGNATURE": signature("w"),
        "RA_CONTEXT_SIGNATURE": signature(candidate.context),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cuda_probe.sys, "argv", ["cuda_probe.py"])
    monkeypatch.setattr(
        cuda_probe, "measure", lambda context, seconds: {"samples_seconds": [0.001] * 10}
    )
    cuda_probe.main()
    line = next(
        x for x in capsys.readouterr().out.splitlines() if x.startswith("RESOURCE_ADVISOR_RESULT ")
    )
    envelope = json.loads(line.split(" ", 1)[1])
    result = ExecutionResult.model_validate(envelope["result"])
    assert signature(result) == envelope["digest"]
    assert result.measurements.peak_memory_mib == 0.015625


def test_timed_qualification_cannot_be_registered_from_short_samples():
    import hashlib
    import runpy
    from pathlib import Path

    import pytest

    build = runpy.run_path(str(Path(__file__).parents[1] / "examples/build_cuda_target.py"))[
        "build"
    ]
    report = {
        "quality_value": 1,
        "elements_checked": cuda_probe.SIZE,
        "kernel_sha256": hashlib.sha256(cuda_probe.PTX).hexdigest(),
        "measurement_boundary": cuda_probe.BOUNDARY + "-timed-90s-windows",
        "samples_seconds": [0.001] * 10,
        "sustained_seconds": 90,
        "kernel_launches": 100,
        "arch": "amd64",
        "runtime_versions": {"cuda_driver_api": "12080"},
        "accelerator_model": "test-device",
    }
    kwargs = {
        "ref": "timed",
        "project": "team-a",
        "node": "test-node",
        "image": "example.test/probe@sha256:" + "1" * 64,
    }
    with pytest.raises(ValueError, match="qualification"):
        build(report, **kwargs)
    report["samples_seconds"] = [9.001] * 10
    bundle = build(report, **kwargs)
    assert bundle["variant"]["command"][-2:] == ["--sustain-seconds", "90"]
    assert bundle["workload"]["execution"]["max_run_seconds"] == 150
    assert bundle["workload"]["candidates"][0]["context"]["parameters"]["sustained_seconds"] == 90
