"""Single reserved-GPU CNN numerical qualification; no trained-model accuracy claim.

Run once under Slurm with one GPU, one CPU and a two-minute limit. Keep failed
allocations and their logs. A successful import or CUDA enumeration is not PASS.
"""

import argparse
import hashlib
import json
import os
import resource
import statistics
import time
import warnings
from pathlib import Path


def memory_boundary(current, root):
    """Observe the tightest ancestor limit, including an unlimited step's job."""
    current, root = current.resolve(), root.resolve()
    if not current.is_relative_to(root):
        raise RuntimeError("cgroup escaped the unified hierarchy")
    limits, swaps = [], []
    for path in (current, *current.parents):
        if not path.is_relative_to(root):
            break
        for filename, values in [("memory.max", limits), ("memory.swap.max", swaps)]:
            file = path / filename
            if file.exists() and (value := file.read_text().strip()) != "max":
                number = int(value)
                if number < 0:
                    raise RuntimeError("negative cgroup limit")
                values.append((number, path))
    if not limits or not swaps:
        raise RuntimeError("finite memory and swap boundaries required")
    limit, path = min(limits, key=lambda item: item[0])
    peak = path / "memory.peak"
    return {
        "memory_max_bytes": limit,
        "swap_max_bytes": min(item[0] for item in swaps),
        "memory_peak_bytes": int(peak.read_text().strip()) if peak.exists() else None,
        "cpu_affinity_count": len(os.sched_getaffinity(0)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-torch", required=True)
    parser.add_argument("--expected-cuda", required=True)
    parser.add_argument("--require-native-arch", action="store_true")
    parser.add_argument("--enforced-memory-mib", type=int)
    args = parser.parse_args()
    if not os.environ.get("SLURM_JOB_ID") or os.environ.get("SLURM_CPUS_PER_TASK") != "1":
        raise RuntimeError("qualification requires a Slurm allocation with one CPU")

    cgroup = None
    if args.enforced_memory_mib is not None:
        membership = [
            line[3:]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        ]
        if len(membership) != 1 or ".." in Path(membership[0]).parts:
            raise RuntimeError("unified cgroup membership is ambiguous")
        cgroup = Path("/sys/fs/cgroup") / membership[0].lstrip("/")
        boundary = memory_boundary(cgroup, Path("/sys/fs/cgroup"))
        if (
            args.enforced_memory_mib <= 0
            or boundary["memory_max_bytes"] != args.enforced_memory_mib * 1024**2
            or boundary["swap_max_bytes"] != 0
            or boundary["cpu_affinity_count"] != 1
        ):
            raise RuntimeError("actual memory/swap/CPU boundaries differ from the fixed plan")
        print("RA_SLURM_TORCH_BOUNDARY " + json.dumps(boundary), flush=True)
    if args.require_native_arch:
        # The strict trial must not hide unsupported-device or other runtime warnings.
        warnings.simplefilter("error", UserWarning)

    import torch

    if torch.__version__ != args.expected_torch or torch.version.cuda != args.expected_cuda:
        raise RuntimeError("framework/CUDA version differs from the fixed qualification")
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("exactly one CUDA device required; refusing CPU fallback")
    if torch.cuda.get_device_capability(0) != (8, 7):
        raise RuntimeError("this qualification is fixed to the Orin compute capability")
    if args.require_native_arch and "sm_87" not in torch.cuda.get_arch_list():
        raise RuntimeError("explicit native sm_87 code is required by this qualification")
    torch.set_num_threads(1)
    torch.manual_seed(20261006)
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    model = torch.nn.Sequential(
        torch.nn.Conv2d(3, 8, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.AvgPool2d(2),
        torch.nn.Conv2d(8, 16, 3, padding=1),
        torch.nn.ReLU(),
        torch.nn.AdaptiveAvgPool2d((1, 1)),
        torch.nn.Flatten(),
        torch.nn.Linear(16, 10),
    ).eval()
    inputs = torch.randn(4, 3, 32, 32, dtype=torch.float32)
    input_hash = hashlib.sha256(inputs.numpy().tobytes()).hexdigest()
    weights_hash = hashlib.sha256(
        b"".join(p.detach().numpy().tobytes() for p in model.parameters())
    ).hexdigest()
    with torch.inference_mode():
        reference = model(inputs)
        model, inputs = model.cuda(), inputs.cuda()
        if not inputs.is_cuda or not all(p.is_cuda for p in model.parameters()):
            raise RuntimeError("model and input must reside on CUDA")
        for _ in range(3):
            model(inputs)
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        samples, max_error = [], 0.0
        for _ in range(10):
            start = time.perf_counter()
            output = model(inputs)
            torch.cuda.synchronize()
            samples.append(time.perf_counter() - start)
            if not output.is_cuda:
                raise RuntimeError("output is not a CUDA tensor")
            observed = output.cpu()
            if not torch.isfinite(observed).all() or not torch.allclose(
                observed, reference, rtol=1e-4, atol=1e-4
            ):
                raise RuntimeError("GPU output differs from the fixed CPU reference")
            max_error = max(max_error, (observed - reference).abs().max().item())
    result = {
        "schema_version": "v1",
        "result": "PASS",
        "scope": "deterministic generated CNN numerical qualification, not model accuracy",
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "device": torch.cuda.get_device_name(0),
        "capability": list(torch.cuda.get_device_capability(0)),
        "cuda_arch_list": torch.cuda.get_arch_list(),
        "input_shape": [4, 3, 32, 32],
        "precision": "fp32",
        "seed": 20261006,
        "input_sha256": input_hash,
        "weights_sha256": weights_hash,
        "warmup_forwards": 3,
        "measured_forwards": 10,
        "compared_elements": reference.numel() * 10,
        "reference_rtol": 1e-4,
        "reference_atol": 1e-4,
        "max_absolute_error": max_error,
        "samples_seconds": samples,
        "median_forward_ms": statistics.median(samples) * 1000,
        "peak_tensor_allocation_mib": torch.cuda.max_memory_allocated() / 1024**2,
        "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "cpu_fallback": False,
    }
    if cgroup is not None:
        result["enforced_boundary"] = memory_boundary(cgroup, Path("/sys/fs/cgroup"))
        if result["peak_process_rss_kib"] * 1024 > args.enforced_memory_mib * 1024**2:
            raise RuntimeError("process RSS exceeds the fixed enforced memory request")
    print("RA_SLURM_TORCH_QUALIFICATION " + json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
