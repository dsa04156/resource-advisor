import json

import pytest
from test_load_context import start

from resource_advisor.load_context import PROVIDER, LoadTrace
from resource_advisor.load_observed_benchmark import observe


def prepared(service, bundle):
    _, _, row, envelope = start(service, bundle)
    record = LoadTrace.model_validate(envelope.pop("load_trace"))
    body = row["body"]
    env = {
        "RA_LOAD_CONTEXT_POLICY": PROVIDER,
        "RA_JOB_ID": row["id"],
        "RA_ATTEMPT_ID": body["attempt_id"],
        "RA_EPOCH": str(body["epoch"]),
        "RA_WORKLOAD_SIGNATURE": body["workload_signature"],
        "RA_CONTEXT_SIGNATURE": body["context_signature"],
        "RA_CONTEXT_JSON": json.dumps(body["candidate"]["context"]),
    }

    class ReaderDouble:
        readings = iter([record.before, record.after])

        def read(self):
            return next(self.readings)

        def trace(self, before, after, result):
            assert before == record.before and after == record.after
            return record

    return env, ReaderDouble(), envelope, record


def test_wrapper_emits_one_bound_envelope_after_reading_both_brackets(service, bundle, capsys):
    env, reader, envelope, trace = prepared(service, bundle)

    def run():
        print("fixture diagnostic")
        print("RESOURCE_ADVISOR_RESULT " + json.dumps(envelope), flush=True)

    result = observe(run, reader=reader, env=env)
    output = capsys.readouterr().out
    assert output.startswith("fixture diagnostic\n")
    assert output.count("RESOURCE_ADVISOR_RESULT ") == 1
    assert result["load_trace"] == trace.model_dump(mode="json")
    assert result["result"] == envelope["result"]


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "digest", "identity", "unbound", "raises"]
)
def test_unchecked_or_failed_result_never_escapes_wrapper(service, bundle, capsys, fault):
    env, reader, envelope, _ = prepared(service, bundle)
    if fault == "digest":
        envelope["digest"] = "sha256:" + "0" * 64
    elif fault == "identity":
        env["RA_JOB_ID"] = "other-job"
    elif fault == "unbound":
        env.pop("RA_LOAD_CONTEXT_POLICY")

    def run():
        if fault == "missing":
            return
        print("RESOURCE_ADVISOR_RESULT " + json.dumps(envelope), flush=True)
        if fault == "duplicate":
            print("RESOURCE_ADVISOR_RESULT " + json.dumps(envelope), flush=True)
        elif fault == "raises":
            raise RuntimeError("workload failed after printing a result")

    with pytest.raises((ValueError, RuntimeError)):
        observe(run, reader=reader, env=env)
    assert "RESOURCE_ADVISOR_RESULT " not in capsys.readouterr().out
