"""Standalone Python 3.10+ ARM CPU producer; no accelerator runtime imports.

--describe reads host identity without computation. Normal execution requires a
qualified native manifest and a Slurm CPU allocation. The fixed scalar FP64
fixture validates every output analytically; it is not a trained-model benchmark.
"""

import hashlib
import json
import math
import os
import platform
import re
import resource
import statistics
import sys
import time
from pathlib import Path


def describe():
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        raise ValueError("this native CPU fixture requires Linux arm64")
    fields = {}
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        key, _, value = line.partition(":")
        if key.strip() in {"CPU implementer", "CPU part"}:
            fields.setdefault(key.strip(), value.strip())
    board = Path("/sys/firmware/devicetree/base/model").read_text().strip("\x00\n")
    if not board or not all(fields.get(k) for k in ("CPU implementer", "CPU part")):
        raise ValueError("actual ARM board/CPU identity is unavailable")
    return {
        "arch": "arm64",
        "accelerator_model": board + " / " + fields["CPU implementer"] + ":" + fields["CPU part"],
        "runtime_versions": {"python": platform.python_version()},
    }


def request_identity(env, host):
    identity = {}
    for field in ("job_id", "attempt_id", "workload_signature", "context_signature"):
        value = env["RA_" + field.upper()]
        pattern = (
            r"sha256:[a-f0-9]{64}"
            if field.endswith("signature")
            else r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"
        )
        if not re.fullmatch(pattern, value):
            raise ValueError("invalid result identity")
        identity[field] = value
    if not re.fullmatch(r"[1-9][0-9]*", env["RA_EPOCH"]):
        raise ValueError("invalid attempt epoch")
    identity["epoch"] = int(env["RA_EPOCH"])
    context = json.loads(env["RA_CONTEXT_JSON"])
    if (
        any(context.get(k) != v for k, v in host.items())
        or context["memory_model"] != "host"
        or context["allocation_mode"] != "cpu_only"
        or context["resources"] != {"host_cpu": 1, "host_memory_mib": 1024, "accelerator_count": 0}
        or context["parameters"] != {}
        or json.loads(env["RA_INPUT_SHAPE"]) != [64, 64]
        or env["RA_WORK_UNITS"] != "5"
        or env["RA_PRECISION"] != "fp64"
        or env["RA_SEED"] != "0"
        or env["RA_EXECUTION_MODE"] not in {"fixed", "observe", "confirmation"}
        or not re.fullmatch(r"[1-9][0-9]*", env.get("SLURM_JOB_ID", ""))
        or env.get("SLURM_CPUS_PER_TASK") != "1"
        or any(
            env.get(k)
            for k in (
                "SLURM_JOB_GPUS",
                "SLURM_STEP_GPUS",
                "CUDA_VISIBLE_DEVICES",
                "RA_SAMPLING_PLAN_JSON",
                "RA_THERMAL_POLICY_JSON",
            )
        )
    ):
        raise ValueError("request differs from the fixed native CPU contract")
    return identity


def compute():
    n = 64
    rows = [[float(i + k) for k in range(n)] for i in range(n)]
    columns = [[float(k - j) for k in range(n)] for j in range(n)]
    return [[sum(x * y for x, y in zip(row, col, strict=True)) for col in columns] for row in rows]


def validate(output):
    n = 64
    total, squares = n * (n - 1) // 2, n * (n - 1) * (2 * n - 1) // 6
    if len(output) != n or any(len(row) != n for row in output):
        raise ValueError("incomplete CPU output")
    if any(
        output[i][j] != squares + (i - j) * total - n * i * j for i in range(n) for j in range(n)
    ):
        raise ValueError("CPU numerical agreement failed")


def run(env, host):
    identity = request_identity(env, host)
    validate(compute())  # Untimed warmup; input construction is included below.
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        output = compute()
        elapsed = time.perf_counter() - start
        validate(output)
        if not math.isfinite(elapsed) or elapsed <= 0:
            raise ValueError("invalid measured CPU duration")
        samples.append(elapsed)
    total, ordered = sum(samples), sorted(samples)
    result = {
        "schema_version": "v1",
        **identity,
        "outcome": "COMPLETED",
        "measured": True,
        "evidence_kind": "hardware",
        "error_code": None,
        "measurements": {
            "elapsed_seconds": total,
            "peak_memory_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "quality_value": 1.0,
            "sample_count": 5,
            "work_units": 5,
            "latency_p50_ms": statistics.median(samples) * 1000,
            "latency_p95_ms": ordered[int(4 * 0.95)] * 1000,
            "latency_p99_ms": ordered[int(4 * 0.99)] * 1000,
            "throughput": 5 / total,
            "gpu_utilization": None,
            "power_watts": None,
            "temperature_celsius": None,
        },
    }
    encoded = json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return {"result": result, "digest": "sha256:" + hashlib.sha256(encoded).hexdigest()}


if __name__ == "__main__":
    if sys.argv[1:] == ["--describe"]:
        print(json.dumps(describe(), sort_keys=True))
    elif len(sys.argv) == 1:
        print("RESOURCE_ADVISOR_RESULT " + json.dumps(run(os.environ, describe()), allow_nan=False))
    else:
        raise ValueError("only --describe or qualified Slurm execution is supported")
