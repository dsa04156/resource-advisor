"""Bounded genuine CPU training with flushed per-epoch progress (standard library only).

One small tanh MLP is trained by mini-batch SGD with explicit backpropagation.
Every epoch is real optimization work; nothing sleeps. The same file runs in the
Resource Advisor container (Kubernetes: copies prepared by the init container)
and natively under Slurm through the operator-installed runtime guard (host
mode: this process verifies the root-owned source and makes per-attempt copies).
It must stay importable without the package so `python3 -I` can execute it.
"""

import argparse
import errno
import hashlib
import json
import math
import os
import random
import resource
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BOUNDARY = "mlp-sgd-cpu-live-v1"
PROGRESS = "RA_PROGRESS "
RESULT = "RESOURCE_ADVISOR_RESULT "
CHECKPOINT_FIELDS = {"model", "w1", "b1", "w2", "b2", "step", "lr", "batch_size", "seed"}


def signature(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fixture(*, seed, samples, features, hidden, lr, batch_size):
    """Deterministic regression data and an untrained checkpoint for registration tooling."""
    rng = random.Random(seed)
    teacher = [rng.uniform(-1, 1) for _ in range(features)]
    x = [[round(rng.uniform(-1, 1), 6) for _ in range(features)] for _ in range(samples)]
    y = [round(math.sin(sum(w * v for w, v in zip(teacher, row, strict=True))), 6) for row in x]
    scale = 1 / math.sqrt(features)
    checkpoint = {
        "model": "tanh-mlp-v1",
        "w1": [[round(rng.gauss(0, scale), 6) for _ in range(hidden)] for _ in range(features)],
        "b1": [0.0] * hidden,
        "w2": [round(rng.gauss(0, 1 / math.sqrt(hidden)), 6) for _ in range(hidden)],
        "b2": 0.0,
        "step": 0,
        "lr": lr,
        "batch_size": batch_size,
        "seed": seed,
    }
    return checkpoint, {"x": x, "y": y}


def verified_bytes(path, digest, size):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("regular source file required")
        data = file.read(size + 1)
    if len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("checkpoint/input size or digest mismatch")
    return data, info


def require_readonly(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        if exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}:
            return True
        raise
    os.close(fd)
    raise RuntimeError("training input unexpectedly writable")


def host_copy(binding, source, work, attempt, owner_uid):
    """Verify operator-owned, read-only originals, then make private per-attempt copies."""
    source_info = os.stat(source, follow_symlinks=False)
    if (
        not stat.S_ISDIR(source_info.st_mode)
        or source_info.st_uid != owner_uid
        or source_info.st_mode & 0o022
        or os.access(source, os.W_OK)
    ):
        raise RuntimeError("training source directory must be operator-owned and read-only")
    root = Path(work) / attempt
    root.mkdir(mode=0o700)  # Never reuse a dirty sandbox from an earlier start.
    paths = {}
    for kind in ("checkpoint", "input"):
        data, info = verified_bytes(
            Path(source) / (kind + ".json"),
            binding[kind + "_digest"],
            binding[kind + "_size_bytes"],
        )
        if info.st_uid != owner_uid or info.st_mode & 0o222 & ~0o444:
            raise RuntimeError("training source file must be operator-owned and read-only")
        require_readonly(Path(source) / (kind + ".json"))
        dest = root / kind
        dest.mkdir(mode=0o700)
        with (dest / (kind + ".json")).open("xb") as file:
            file.write(data)
        (dest / (kind + ".json")).chmod(0o400)
        dest.chmod(0o500)
        paths[kind] = dest / (kind + ".json")
    (root / "output").mkdir(mode=0o700)
    return paths["checkpoint"], paths["input"], root / "output"


def forward(model, row):
    hidden = [
        math.tanh(model["b1"][j] + sum(row[i] * model["w1"][i][j] for i in range(len(row))))
        for j in range(len(model["b1"]))
    ]
    return hidden, model["b2"] + sum(h * w for h, w in zip(hidden, model["w2"], strict=True))


def mean_loss(model, x, y):
    return sum((forward(model, row)[1] - target) ** 2 for row, target in zip(x, y, strict=True)) / len(y)


def train_epoch(model, x, y, order):
    lr, size = model["lr"], model["batch_size"]
    features, width = len(x[0]), len(model["b1"])
    for start in range(0, len(order), size):
        batch = order[start : start + size]
        g1 = [[0.0] * width for _ in range(features)]
        gb1, g2, gb2 = [0.0] * width, [0.0] * width, 0.0
        for index in batch:
            row = x[index]
            hidden, output = forward(model, row)
            delta = 2 * (output - y[index]) / len(batch)
            gb2 += delta
            for j in range(width):
                g2[j] += delta * hidden[j]
                back = delta * model["w2"][j] * (1 - hidden[j] ** 2)
                gb1[j] += back
                for i in range(features):
                    g1[i][j] += back * row[i]
        for j in range(width):
            model["w2"][j] -= lr * g2[j]
            model["b1"][j] -= lr * gb1[j]
            for i in range(features):
                model["w1"][i][j] -= lr * g1[i][j]
        model["b2"] -= lr * gb2
        model["step"] += 1


def emit(prefix, value):
    print(prefix + json.dumps(value, separators=(",", ":"), allow_nan=False), flush=True)


def run(*, binding, checkpoint_path, input_path, output_dir, protection, environ=os.environ):
    context = json.loads(environ["RA_CONTEXT_JSON"])
    if (
        context["allocation_mode"] != "cpu_only"
        or context["resources"]["accelerator_count"] != 0
        or context["parameters"]
    ):
        raise RuntimeError("live CPU training requires a CPU-only allocation and fixed semantics")
    epochs = int(environ["RA_WORK_UNITS"])
    if environ["RA_PRECISION"] != "fp64" or not 1 <= epochs <= 100000:
        raise ValueError("live CPU training uses float64 and a bounded epoch count")
    checkpoint_raw, _ = verified_bytes(
        checkpoint_path, binding["checkpoint_digest"], binding["checkpoint_size_bytes"]
    )
    input_raw, _ = verified_bytes(input_path, binding["input_digest"], binding["input_size_bytes"])
    readonly = require_readonly(checkpoint_path), require_readonly(input_path)
    initial, data = json.loads(checkpoint_raw), json.loads(input_raw)
    if set(initial) != CHECKPOINT_FIELDS or initial["model"] != "tanh-mlp-v1" or initial["step"]:
        raise ValueError("unsupported or already-trained checkpoint")
    x, y = data["x"], data["y"]
    if json.loads(environ["RA_INPUT_SHAPE"]) != [len(x), len(x[0])] or len(y) != len(x):
        raise ValueError("input shape differs from the qualified workload")
    model = json.loads(checkpoint_raw)
    rng = random.Random(initial["seed"] + int(environ["RA_SEED"]))
    attempt, job = environ["RA_ATTEMPT_ID"], environ["RA_JOB_ID"]
    started = time.perf_counter()
    first_loss = mean_loss(model, x, y)
    emit(
        PROGRESS,
        {
            "schema": "ra-progress-v1",
            "job_id": job,
            "attempt_id": attempt,
            "epoch": 0,
            "total_epochs": epochs,
            "optimizer_steps": 0,
            "loss": first_loss,
            "elapsed_seconds": 0.0,
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )
    epoch_seconds = []
    for epoch in range(1, epochs + 1):
        begin = time.perf_counter()
        order = list(range(len(x)))
        rng.shuffle(order)
        train_epoch(model, x, y, order)
        loss = mean_loss(model, x, y)
        if not math.isfinite(loss):
            raise FloatingPointError("training diverged")
        epoch_seconds.append(time.perf_counter() - begin)
        emit(
            PROGRESS,
            {
                "schema": "ra-progress-v1",
                "job_id": job,
                "attempt_id": attempt,
                "epoch": epoch,
                "total_epochs": epochs,
                "optimizer_steps": model["step"],
                "loss": loss,
                "elapsed_seconds": time.perf_counter() - started,
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )
    elapsed = time.perf_counter() - started
    variance = sum((v - sum(y) / len(y)) ** 2 for v in y) / len(y)
    # Rounding keeps the bounded JSON checkpoint exact across encoders.
    output = {
        key: (
            [[round(v, 12) for v in row] for row in value]
            if key == "w1"
            else [round(v, 12) for v in value]
            if key in {"b1", "w2"}
            else round(value, 12)
            if key == "b2"
            else value
        )
        for key, value in model.items()
    }
    if signature(output) == signature(initial):
        raise RuntimeError("training produced no parameter change")
    verified_bytes(checkpoint_path, binding["checkpoint_digest"], binding["checkpoint_size_bytes"])
    verified_bytes(input_path, binding["input_digest"], binding["input_size_bytes"])
    with (Path(output_dir) / "checkpoint.json").open("x") as file:
        file.write(encode(output).decode())
    ordered = sorted(epoch_seconds)
    result = {
        "schema_version": "v1",
        "job_id": job,
        "attempt_id": attempt,
        "epoch": int(environ["RA_EPOCH"]),
        "workload_signature": environ["RA_WORKLOAD_SIGNATURE"],
        "context_signature": environ["RA_CONTEXT_SIGNATURE"],
        "outcome": "COMPLETED",
        "measured": True,
        "evidence_kind": "hardware",
        "measurements": {
            "elapsed_seconds": float(elapsed),
            "peak_memory_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "quality_value": float(1 - mean_loss(model, x, y) / variance),
            "sample_count": epochs,
            "work_units": epochs,
            "latency_p50_ms": ordered[len(ordered) // 2] * 1000,
            "latency_p95_ms": ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
            * 1000,
            "latency_p99_ms": None,
            "throughput": float(epochs * len(x) / elapsed),
            "gpu_utilization": None,
            "power_watts": None,
            "temperature_celsius": None,
        },
        "error_code": None,
    }
    receipt = {
        "job_id": job,
        "attempt_id": attempt,
        "result_digest": signature(result),
        "initial_checkpoint_digest": binding["checkpoint_digest"],
        "input_digest": binding["input_digest"],
        "checkpoint_after_digest": binding["checkpoint_digest"],
        "input_after_digest": binding["input_digest"],
        "checkpoint_readonly": readonly[0],
        "input_readonly": readonly[1],
        **protection,
        "output_checkpoint_digest": signature(output),
        "output_checkpoint": output,
    }
    emit(RESULT, {"result": result, "digest": signature(result), "training_receipt": receipt})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-source", help="operator-owned read-only source directory")
    parser.add_argument("--host-work", help="per-attempt private work root")
    parser.add_argument("--source-owner-uid", type=int, default=0)
    args = parser.parse_args(argv)
    binding = json.loads(os.environ["RA_TRAINING_BINDING"])
    if args.host_source:
        if binding.get("backend") != "slurm" or not args.host_work:
            raise RuntimeError("host copy mode requires a Slurm training binding and work root")
        checkpoint, inputs, output = host_copy(
            binding, args.host_source, args.host_work, os.environ["RA_ATTEMPT_ID"],
            args.source_owner_uid,
        )
        protection = {"source_protection": "operator-owned-readonly-host-copy"}
    else:
        if Path("/ra-source").exists():
            raise RuntimeError("original source is exposed to training container")
        checkpoint = Path("/ra-checkpoint/checkpoint.json")
        inputs = Path("/ra-input/input.json")
        output = Path("/ra-output")
        protection = {"source_not_mounted": True}
    run(
        binding=binding,
        checkpoint_path=checkpoint,
        input_path=inputs,
        output_dir=output,
        protection=protection,
    )


if __name__ == "__main__":
    sys.exit(main())
