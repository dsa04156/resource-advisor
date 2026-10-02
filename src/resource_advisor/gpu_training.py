"""Bounded deterministic GPU training fixture with explicit SGD state restoration.

This is not arbitrary user training, convergence validation, or automatic resume.
Only JSON tensor lists are read; executable checkpoint deserialization is not used.
"""

import errno
import json
import os
import platform
import statistics
import time
from pathlib import Path

from .contracts import ExecutionResult, Measurements, signature
from .training import TrainingIsolation, TrainingReceipt, verified_bytes

BOUNDARY = "linear-sgd-ten-step-synchronized-v1"


def require_readonly(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        if exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}:
            return True
        raise
    else:
        os.close(fd)
        raise RuntimeError("training input unexpectedly writable")


def run():
    import torch

    binding = TrainingIsolation.model_validate_json(os.environ["RA_TRAINING_BINDING"])
    context = json.loads(os.environ["RA_CONTEXT_JSON"])
    if context["parameters"] or context["allocation_mode"] != "physical_device":
        raise RuntimeError("training fixture requires fixed semantics and a physical GPU")
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("one CUDA GPU required; no CPU fallback")
    if context["resources"]["accelerator_count"] != 1:
        raise RuntimeError("one GPU allocation required")
    actual = {"cuda": torch.version.cuda, "pytorch": torch.__version__}
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if arch != context["arch"] or torch.cuda.get_device_name(0) != context["accelerator_model"]:
        raise RuntimeError("hardware differs from qualification")
    if any(actual.get(k) != v for k, v in context["runtime_versions"].items()):
        raise RuntimeError("runtime differs from qualification")
    if (
        os.environ["RA_WORK_UNITS"] != "10"
        or os.environ["RA_PRECISION"] != "fp32"
        or json.loads(os.environ["RA_INPUT_SHAPE"]) != [8, 2]
    ):
        raise ValueError("qualified fixture requires ten FP32 steps and 8x2 input")
    checkpoint_path = Path("/ra-checkpoint/checkpoint.json")
    input_path = Path("/ra-input/input.json")
    checkpoint = json.loads(
        verified_bytes(checkpoint_path, binding.checkpoint_digest, binding.checkpoint_size_bytes)
    )
    inputs = json.loads(verified_bytes(input_path, binding.input_digest, binding.input_size_bytes))
    checkpoint_ro, input_ro = require_readonly(checkpoint_path), require_readonly(input_path)
    if Path("/ra-source").exists():
        raise RuntimeError("original source is exposed to training container")
    if set(checkpoint) != {
        "weights",
        "bias",
        "momentum_weights",
        "momentum_bias",
        "step",
        "lr",
        "momentum",
    }:
        raise ValueError("unsupported checkpoint fields")
    if checkpoint["lr"] != 0.05 or checkpoint["momentum"] != 0.9 or checkpoint["step"] != 0:
        raise ValueError("fixture only qualifies initial step zero with fixed SGD hyperparameters")
    torch.set_num_threads(max(1, int(context["resources"]["host_cpu"])))
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.use_deterministic_algorithms(True)

    def train(device, timed):
        weights = torch.tensor(
            checkpoint["weights"], dtype=torch.float32, device=device, requires_grad=True
        )
        bias = torch.tensor(
            checkpoint["bias"], dtype=torch.float32, device=device, requires_grad=True
        )
        mw = torch.tensor(checkpoint["momentum_weights"], dtype=torch.float32, device=device)
        mb = torch.tensor(checkpoint["momentum_bias"], dtype=torch.float32, device=device)
        x = torch.tensor(inputs["x"], dtype=torch.float32, device=device)
        y = torch.tensor(inputs["y"], dtype=torch.float32, device=device)
        if x.shape != (8, 2) or y.shape != (8, 1) or weights.shape != (2, 1) or bias.shape != (1,):
            raise ValueError("unexpected fixture tensor shapes")
        if mw.shape != weights.shape or mb.shape != bias.shape:
            raise ValueError("optimizer state shape mismatch")
        tensors = [weights, bias, mw, mb, x, y]
        if not all(torch.isfinite(t).all().item() for t in tensors):
            raise ValueError("non-finite fixture data")
        optimizer = torch.optim.SGD([weights, bias], lr=0.05, momentum=0.9, foreach=False)
        optimizer.state[weights]["momentum_buffer"] = mw.clone()
        optimizer.state[bias]["momentum_buffer"] = mb.clone()
        if timed:
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
        samples = []
        for _ in range(10):
            start = time.perf_counter()
            optimizer.zero_grad(set_to_none=True)
            loss = ((x @ weights + bias - y) ** 2).mean()
            loss.backward()
            optimizer.step()
            if timed:
                torch.cuda.synchronize()
            samples.append(time.perf_counter() - start)
        final = {
            "weights": weights.detach().cpu().tolist(),
            "bias": bias.detach().cpu().tolist(),
            "momentum_weights": optimizer.state[weights]["momentum_buffer"].cpu().tolist(),
            "momentum_bias": optimizer.state[bias]["momentum_buffer"].cpu().tolist(),
            "step": 10,
            "lr": 0.05,
            "momentum": 0.9,
        }
        return final, samples

    # Warmup uses a separate state, then the measured run starts from the original again.
    train("cuda", False)
    torch.cuda.synchronize()
    output, samples = train("cuda", True)
    peak = torch.cuda.max_memory_allocated() / 1024**2
    reference, _ = train("cpu", False)
    agrees = []
    for key in ("weights", "bias", "momentum_weights", "momentum_bias"):
        agrees.extend(
            torch.isclose(
                torch.tensor(output[key]), torch.tensor(reference[key]), rtol=1e-4, atol=1e-5
            )
            .flatten()
            .tolist()
        )
    quality = sum(agrees) / len(agrees)
    verified_bytes(checkpoint_path, binding.checkpoint_digest, binding.checkpoint_size_bytes)
    verified_bytes(input_path, binding.input_digest, binding.input_size_bytes)
    with Path("/ra-output/checkpoint.json").open("x") as file:
        file.write(json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False))
    result = ExecutionResult(
        job_id=os.environ["RA_JOB_ID"],
        attempt_id=os.environ["RA_ATTEMPT_ID"],
        epoch=int(os.environ["RA_EPOCH"]),
        workload_signature=os.environ["RA_WORKLOAD_SIGNATURE"],
        context_signature=os.environ["RA_CONTEXT_SIGNATURE"],
        outcome="COMPLETED",
        evidence_kind="hardware",
        measurements=Measurements(
            elapsed_seconds=sum(samples),
            peak_memory_mib=peak,
            quality_value=quality,
            sample_count=10,
            work_units=10,
            latency_p50_ms=statistics.median(samples) * 1000,
            throughput=10 / sum(samples),
        ),
    )
    receipt = TrainingReceipt(
        job_id=result.job_id,
        attempt_id=result.attempt_id,
        result_digest=signature(result),
        initial_checkpoint_digest=binding.checkpoint_digest,
        input_digest=binding.input_digest,
        checkpoint_after_digest=binding.checkpoint_digest,
        input_after_digest=binding.input_digest,
        checkpoint_readonly=checkpoint_ro,
        input_readonly=input_ro,
        source_not_mounted=True,
        output_checkpoint_digest=signature(output),
        output_checkpoint=output,
    )
    print(
        "RESOURCE_ADVISOR_RESULT "
        + json.dumps(
            {
                "result": result.model_dump(mode="json"),
                "digest": signature(result),
                "training_receipt": receipt.model_dump(mode="json"),
            },
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    run()
