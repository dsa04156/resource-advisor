"""Bounded same-model CUDA CNN trial with serial E2E phase instrumentation.

Random weights/data are numerical test fixtures, not a trained classifier.
One input is intentionally reused. The approved cache option reuses its CPU
preprocessing, including the first cache fill in the timed boundary.
"""

import copy
import hashlib
import json
import os
import platform
import statistics
import time

from .contracts import ExecutionResult, Measurements, signature
from .diagnostics import PhaseProfile, PhaseSample

BOUNDARY = "cnn-serial-input-transfer-forward-v1"
MODEL_RECIPE = "conv3-32-relu-conv32-32-relu-conv32-32-relu-globalavg-v1"


def run():
    import torch

    context = json.loads(os.environ["RA_CONTEXT_JSON"])
    shape = json.loads(os.environ["RA_INPUT_SHAPE"])
    iterations = int(os.environ["RA_WORK_UNITS"])
    strategy = context["parameters"].get("input_strategy", "recompute")
    if strategy not in {"recompute", "cache"} or set(context["parameters"]) - {"input_strategy"}:
        raise ValueError("only recompute/cache input_strategy is supported")
    if shape != [1, 3, 128, 128] or not 3 <= iterations <= 30:
        raise ValueError("qualified boundary is 1x3x128x128 and 3–30 iterations")
    if os.environ["RA_PRECISION"] != "fp32":
        raise ValueError("only fp32 qualified")
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("exactly one CUDA GPU required; no CPU fallback")
    if context["resources"]["accelerator_count"] != 1:
        raise RuntimeError("one allocated GPU required")
    if context["allocation_mode"] != "physical_device":
        raise RuntimeError("shared execution is not qualified")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if arch != context["arch"] or torch.cuda.get_device_name(0) != context["accelerator_model"]:
        raise RuntimeError("hardware differs from approved context")
    actual = {"pytorch": torch.__version__, "cuda": torch.version.cuda}
    if any(actual.get(k) != v for k, v in context["runtime_versions"].items()):
        raise RuntimeError("runtime differs from qualified context")
    torch.set_num_threads(max(1, int(context["resources"]["host_cpu"])))
    torch.manual_seed(int(os.environ["RA_SEED"]))
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    model_cpu = torch.nn.Sequential(
        torch.nn.Conv2d(3, 32, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.Conv2d(32, 32, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.Conv2d(32, 32, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.AdaptiveAvgPool2d(1),
    ).eval()
    raw = torch.randn(*shape)

    def preprocess():
        # Always start from raw. Repetition cannot change the logical input.
        return torch.nn.functional.avg_pool2d(raw, 3, stride=1, padding=1).contiguous()

    with torch.inference_mode():
        reference = model_cpu(preprocess()).double()
        fixture = {
            "model_weights_digest": "sha256:"
            + hashlib.sha256(
                b"".join(t.contiguous().numpy().tobytes() for t in model_cpu.state_dict().values())
            ).hexdigest(),
            "input_digest": "sha256:" + hashlib.sha256(raw.numpy().tobytes()).hexdigest(),
            "prepared_input_digest": "sha256:"
            + hashlib.sha256(preprocess().numpy().tobytes()).hexdigest(),
        }
        print("RA_CNN_FIXTURE " + json.dumps(fixture), flush=True)
        model = copy.deepcopy(model_cpu).cuda()
        for _ in range(3):
            model(preprocess().cuda())
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        samples = []
        cached = None
        for _ in range(iterations):
            start = time.perf_counter()
            if strategy == "recompute" or cached is None:
                prepared = preprocess()
                if strategy == "cache":
                    cached = prepared
            else:
                prepared = cached
            input_done = time.perf_counter()
            data = prepared.cuda()
            torch.cuda.synchronize()
            transfer_done = time.perf_counter()
            output = model(data)
            torch.cuda.synchronize()
            done = time.perf_counter()
            samples.append(
                PhaseSample(
                    wall_seconds=done - start,
                    phases_seconds={
                        "cpu_processing": input_done - start,
                        "host_to_device": transfer_done - input_done,
                        "accelerator_compute": done - transfer_done,
                    },
                )
            )
        quality = torch.isclose(output.cpu().double(), reference, atol=1e-4, rtol=1e-4)
        quality = quality.double().mean().item()
    wall = [s.wall_seconds for s in samples]
    ordered = sorted(wall)
    elapsed = sum(wall)
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
            latency_p50_ms=statistics.median(wall) * 1000,
            latency_p95_ms=ordered[int((len(wall) - 1) * 0.95)] * 1000,
            latency_p99_ms=ordered[int((len(wall) - 1) * 0.99)] * 1000,
            throughput=iterations / elapsed,
        ),
    )
    phases = PhaseProfile(
        job_id=result.job_id,
        attempt_id=result.attempt_id,
        result_digest=signature(result),
        measurement_boundary=BOUNDARY,
        samples=tuple(samples),
    )
    print(
        "RESOURCE_ADVISOR_RESULT "
        + json.dumps(
            {
                "result": result.model_dump(mode="json"),
                "digest": signature(result),
                "phase_profile": phases.model_dump(mode="json"),
            },
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    run()
