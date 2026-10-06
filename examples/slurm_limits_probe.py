"""Bounded, allocation-owned Slurm cgroup acceptance probes; never host stress."""

import argparse
import errno
import hashlib
import json
import os
import runpy
from pathlib import Path


def effective_limit(start, root, filename):
    """Return the tightest inherited finite cgroup limit, never invent a bound."""
    if not start.is_relative_to(root):
        raise RuntimeError("cgroup escaped the unified hierarchy")
    values = []
    for path in (start, *start.parents):
        if not path.is_relative_to(root):
            break
        limit = path / filename
        if limit.is_file():
            value = limit.read_text().strip()
            if value != "max":
                number = int(value)
                if number < 0:
                    raise RuntimeError("invalid cgroup limit")
                values.append(number)
        if path == root:
            break
    return min(values) if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["gpu-positive", "gpu-denied", "oom"], required=True)
    parser.add_argument("--cuda-probe")
    parser.add_argument("--cuda-probe-sha256")
    args = parser.parse_args()
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("a real Slurm allocation is required")
    roots = [
        line[3:]
        for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    ]
    if len(roots) != 1 or ".." in Path(roots[0]).parts:
        raise RuntimeError("unified cgroup membership is ambiguous")
    root = Path("/sys/fs/cgroup")
    current = root / roots[0].lstrip("/")
    limit = effective_limit(current, root, "memory.max")
    swap = effective_limit(current, root, "memory.swap.max")
    expected_mib = {"gpu-positive": 512, "gpu-denied": 256, "oom": 64}[args.mode]
    cpus = len(os.sched_getaffinity(0))
    if limit != expected_mib * 1024**2 or swap != 0 or cpus != 1:
        raise RuntimeError("actual inherited memory/swap/CPU limits differ from the fixed plan")
    print(
        "RA_SLURM_LIMITS "
        + json.dumps({"mode": args.mode, "memory_max": limit, "swap_max": swap, "cpus": cpus}),
        flush=True,
    )
    if args.mode == "oom":
        # The confirmed 64 MiB cgroup must kill this one process before it can
        # consume its bounded 160 MiB request. No allocation occurs without it.
        chunks = []
        for _ in range(160):
            chunks.append(bytearray(1024**2))
            for offset in range(0, len(chunks[-1]), 4096):
                chunks[-1][offset] = 1
        raise RuntimeError("memory cap did not kill the deliberately bounded process")
    if not args.cuda_probe or not args.cuda_probe_sha256:
        raise RuntimeError("pinned CUDA kernel probe required")
    source = Path(args.cuda_probe)
    if hashlib.sha256(source.read_bytes()).hexdigest() != args.cuda_probe_sha256:
        raise RuntimeError("CUDA probe source changed")
    if args.mode == "gpu-positive":
        runpy.run_path(str(source), run_name="__main__")
        return
    try:
        fd = os.open("/dev/nvidia0", os.O_RDWR)
    except OSError as exc:
        if exc.errno != errno.EPERM:
            raise RuntimeError("GPU denial was not a cgroup permission denial") from exc
    else:
        os.close(fd)
        raise RuntimeError("unallocated managed GPU device was accessible")
    try:
        runpy.run_path(str(source), run_name="__main__")
    except RuntimeError as exc:
        text = str(exc)
        if not (
            text.startswith("cuInit failed with CUDA error")
            or text.startswith("cuDeviceGetCount failed with CUDA error")
            or text.startswith("cuCtxCreate_v2 failed with CUDA error")
            or text == "F0 requires exactly one visible physical CUDA device"
        ):
            raise
        print("RA_GPU_DENIED " + json.dumps({"device_errno": errno.EPERM, "cuda_error": text}))
    else:
        raise RuntimeError("CUDA kernel executed without a GPU reservation")


if __name__ == "__main__":
    main()
