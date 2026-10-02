"""Cooperative, bounded PyTorch CUDA matmul workload. No CPU fallback.

Numerical agreement is a benchmark correctness check, not model accuracy.
The image/environment containing PyTorch must be independently qualified.
"""

import json
import os
import platform
import statistics
import time

from .contracts import ExecutionResult, Measurements, signature


def run():
    import torch

    context = json.loads(os.environ["RA_CONTEXT_JSON"])
    shape = json.loads(os.environ["RA_INPUT_SHAPE"])
    iterations = int(os.environ["RA_WORK_UNITS"])
    precision = os.environ["RA_PRECISION"]
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable; refusing CPU fallback")
    if context["resources"]["accelerator_count"] != 1 or torch.cuda.device_count() != 1:
        raise RuntimeError("benchmark requires exactly one visible allocated GPU")
    if len(shape) != 2 or shape[0] != shape[1] or not 16 <= shape[0] <= 2048:
        raise ValueError("initial benchmark contract requires a square matrix of size 16–2048")
    if not 3 <= iterations <= 100 or precision not in {"fp32", "fp16"}:
        raise ValueError("initial benchmark contract requires 3–100 iterations and fp32/fp16")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if arch != context["arch"] or torch.cuda.get_device_name(0) != context["accelerator_model"]:
        raise RuntimeError("actual hardware differs from approved execution context")
    actual = {"pytorch": torch.__version__, "cuda": torch.version.cuda}
    for key, value in context["runtime_versions"].items():
        if actual.get(key) != value:
            raise RuntimeError(f"runtime version cannot be verified: {key}")
    if context["allocation_mode"] != "physical_device":
        raise RuntimeError("shared GPU benchmark qualification is not implemented")
    torch.set_num_threads(max(1, int(context["resources"]["host_cpu"])))
    torch.manual_seed(int(os.environ["RA_SEED"]))
    torch.backends.cuda.matmul.allow_tf32 = False
    dtype = torch.float32 if precision == "fp32" else torch.float16
    a_cpu, b_cpu = torch.randn(*shape, dtype=dtype), torch.randn(*shape, dtype=dtype)
    reference = a_cpu.double() @ b_cpu.double()
    a, b = a_cpu.cuda(), b_cpu.cuda()
    with torch.inference_mode():
        for _ in range(3):
            output = a @ b
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        samples = []
        for _ in range(iterations):
            start = time.perf_counter()
            output = a @ b
            torch.cuda.synchronize()
            samples.append(time.perf_counter() - start)
    actual_output = output.cpu().double()
    tolerance = 0.01 if precision == "fp16" else 1e-4
    quality = (
        torch.isclose(actual_output, reference, rtol=tolerance, atol=tolerance)
        .double()
        .mean()
        .item()
    )
    ordered = sorted(samples)

    def percentile(q):
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))] * 1000

    elapsed = sum(samples)
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
            peak_memory_mib=torch.cuda.max_memory_allocated() / 1024**2,
            quality_value=quality,
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


if __name__ == "__main__":
    run()
