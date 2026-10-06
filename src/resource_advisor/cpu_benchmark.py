"""Bounded Linux CPU computation with an independent analytical correctness check.

This scalar Python FP64 fixture measures CPU matrix multiplication, not PyTorch
or a model's accuracy. It never imports a GPU runtime or performs CUDA fallback.
"""

import json
import os
import platform
import resource
import statistics
import time
from pathlib import Path

from .contracts import ExecutionResult, Measurements, signature


def cpu_model():
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "model name" and value.strip():
            return value.strip()
    raise RuntimeError("CPU model cannot be verified by this initial Linux fixture")


def multiply(n):
    a = [[float(i + k) for k in range(n)] for i in range(n)]
    columns = [[float(k - j) for k in range(n)] for j in range(n)]
    return [
        [sum(x * y for x, y in zip(row, column, strict=True)) for column in columns] for row in a
    ]


def numerical_agreement(output, n):
    # Sum((i+k)*(k-j)) = sum(k²) + (i-j)*sum(k) - n*i*j.
    # This oracle does not call the multiplication implementation.
    k_sum, k_squared = n * (n - 1) // 2, n * (n - 1) * (2 * n - 1) // 6
    return (
        sum(
            output[i][j] == k_squared + (i - j) * k_sum - n * i * j
            for i in range(n)
            for j in range(n)
        )
        / n**2
    )


def run():
    context = json.loads(os.environ["RA_CONTEXT_JSON"])
    shape = json.loads(os.environ["RA_INPUT_SHAPE"])
    iterations = int(os.environ["RA_WORK_UNITS"])
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("this initial CPU fixture requires Linux amd64")
    if (
        context["arch"] != "amd64"
        or context["allocation_mode"] != "cpu_only"
        or context["resources"]["accelerator_count"] != 0
        or context["memory_model"] != "host"
        or context["accelerator_model"] != cpu_model()
    ):
        raise RuntimeError("actual CPU differs from the approved CPU-only execution context")
    if (
        os.environ.get("NVIDIA_VISIBLE_DEVICES") != "void"
        or os.environ.get("CUDA_VISIBLE_DEVICES") != ""
    ):
        raise RuntimeError("explicit CPU-only GPU visibility mask required")
    if context["runtime_versions"] != {"python": platform.python_version()}:
        raise RuntimeError("CPU Python runtime differs from the qualified version")
    if len(shape) != 2 or shape[0] != shape[1] or not 16 <= shape[0] <= 128:
        raise ValueError("CPU fixture requires a square matrix of size 16–128")
    if not 3 <= iterations <= 20 or os.environ["RA_PRECISION"] != "fp64":
        raise ValueError("CPU fixture requires 3–20 iterations and fp64")
    if int(os.environ["RA_SEED"]) != 0:
        raise ValueError("CPU fixture has fixed analytical inputs and seed0")
    n = shape[0]
    multiply(n)  # One untimed warmup; input construction is inside the timed calls.
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        output = multiply(n)
        samples.append(time.perf_counter() - started)
    ordered, elapsed = sorted(samples), sum(samples)

    def percentile(q):
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))] * 1000

    result = ExecutionResult(
        job_id=os.environ["RA_JOB_ID"],
        attempt_id=os.environ["RA_ATTEMPT_ID"],
        epoch=int(os.environ["RA_EPOCH"]),
        workload_signature=os.environ["RA_WORKLOAD_SIGNATURE"],
        context_signature=os.environ["RA_CONTEXT_SIGNATURE"],
        outcome="COMPLETED",
        evidence_kind="hardware",
        measurements=Measurements(
            elapsed_seconds=elapsed,
            peak_memory_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            quality_value=numerical_agreement(output, n),
            sample_count=iterations,
            work_units=iterations,
            latency_p50_ms=statistics.median(samples) * 1000,
            latency_p95_ms=percentile(0.95),
            latency_p99_ms=percentile(0.99),
            throughput=iterations / elapsed,
        ),
    )
    print(
        "RESOURCE_ADVISOR_RESULT "
        + json.dumps(
            {"result": result.model_dump(mode="json"), "digest": signature(result)}, allow_nan=False
        )
    )
    return result


if __name__ == "__main__":
    run()
