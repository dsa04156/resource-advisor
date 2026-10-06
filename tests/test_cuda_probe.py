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
    monkeypatch.setattr(cuda_probe, "measure", lambda context: {"samples_seconds": [0.001] * 10})
    cuda_probe.main()
    line = next(
        x for x in capsys.readouterr().out.splitlines() if x.startswith("RESOURCE_ADVISOR_RESULT ")
    )
    envelope = json.loads(line.split(" ", 1)[1])
    result = ExecutionResult.model_validate(envelope["result"])
    assert signature(result) == envelope["digest"]
    assert result.measurements.peak_memory_mib == 0.015625
