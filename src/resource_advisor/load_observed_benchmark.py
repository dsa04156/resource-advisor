"""Opt-in cgroup observation around the qualified CUDA transfer fixture.

Optional preparation delay permits a bounded lab intervention on this Pod. It
is outside useful compute and the load window, but inside scheduler allocation.
"""

import argparse
import contextlib
import json
import os
import sys
import time

from .contracts import ExecutionResult, signature
from .load_context import PROVIDER, CgroupReader, validate_trace


class ResultStream:
    """Forward diagnostic lines but emit the single result only after telemetry checks."""

    def __init__(self, target):
        self.target, self.pending, self.envelope = target, "", None

    def write(self, text):
        self.pending += text
        if len(self.pending) > 65536:
            raise ValueError("runner output line exceeds limit")
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.startswith("RESOURCE_ADVISOR_RESULT "):
                if self.envelope is not None:
                    raise ValueError("multiple result envelopes")
                self.envelope = json.loads(line.split(" ", 1)[1])
            else:
                self.target.write(line + "\n")
        return len(text)

    def flush(self):
        self.target.flush()


def observe(run, *, reader=None, env=None):
    env = os.environ if env is None else env
    if env.get("RA_LOAD_CONTEXT_POLICY") != PROVIDER:
        raise ValueError("explicit qualified load-context policy required")
    reader = CgroupReader() if reader is None else reader
    before = reader.read()
    output = ResultStream(sys.stdout)
    with contextlib.redirect_stdout(output):
        run()
        if output.pending:
            output.write("\n")
    after = reader.read()
    if output.envelope is None:
        raise ValueError("runner did not emit an execution result")
    envelope = output.envelope
    result = ExecutionResult.model_validate(envelope["result"])
    if (
        envelope["digest"] != signature(result)
        or result.job_id != env["RA_JOB_ID"]
        or result.attempt_id != env["RA_ATTEMPT_ID"]
        or result.epoch != int(env["RA_EPOCH"])
        or result.workload_signature != env["RA_WORKLOAD_SIGNATURE"]
        or result.context_signature != env["RA_CONTEXT_SIGNATURE"]
        or result.outcome != "COMPLETED"
        or "load_trace" in envelope
    ):
        raise ValueError("load-observed result identity or outcome mismatch")
    trace = reader.trace(before, after, result)
    validate_trace(
        trace,
        result,
        {
            "variant": {"load_context_policy": PROVIDER},
            "candidate": {"context": json.loads(env["RA_CONTEXT_JSON"])},
        },
    )
    envelope["load_trace"] = trace.model_dump(mode="json")
    encoded = json.dumps(envelope, allow_nan=False)
    if len(encoded) > 65536:
        raise ValueError("load-observed envelope exceeds collector limit")
    print("RESOURCE_ADVISOR_RESULT " + encoded, flush=True)
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation-seconds", type=int, choices=range(31), default=0)
    args = parser.parse_args()
    if os.environ.get("RA_LOAD_CONTEXT_POLICY") != PROVIDER:
        raise ValueError("load context was not requested by a qualified variant")
    import torch

    from .transfer_gpu_benchmark import run

    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("one allocated CUDA GPU required; no CPU fallback")
    torch.cuda.init()
    print("RA_LOAD_OBSERVATION_READY", flush=True)
    time.sleep(args.preparation_seconds)
    observe(run)


if __name__ == "__main__":
    main()
