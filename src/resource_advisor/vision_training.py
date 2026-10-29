"""Bounded genuine PyTorch vision training on a manifest-pinned real-image subset.

torchvision mobilenet_v3_small or resnet18 is built from a seeded initialization
(weights=None, no download) and trained with AdamW for a fixed number of optimizer
steps at a fixed global batch. Every step is a real forward/backward/update on
pinned ImageNetV2 images; there is no synthetic input and no forward-only mode.

Signed semantics (steps, global batch, input shape, precision, seed, model,
dataset manifest digest, initial checkpoint digest, runtime versions, device
name) are checked against the Resource Advisor environment before any work.
DDP is opt-in (`--strategy ddp` under torchrun): the global batch is split
across ranks, every rank starts from the same seeded weights, gradient
all-reduce happens inside the timed loop, only rank 0 emits the result, and any
rank failure aborts the process group. The default is one device.

The file imports only the standard library at module level so `python -I` can
run it from an image without the resource_advisor package.
"""

import argparse
import errno
import hashlib
import json
import math
import os
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BOUNDARY = "vision-adamw-fixed-steps-synchronized-v1"
PROGRESS = "RA_PROGRESS "
RESULT = "RESOURCE_ADVISOR_RESULT "
MODELS = ("mobilenet_v3_small", "resnet18")
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)
MAX_STEPS = 100


def signature(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def emit(prefix, value):
    print(prefix + json.dumps(value, separators=(",", ":"), allow_nan=False), flush=True)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(root, digest, manifest=None):
    """Verify the dataset manifest's canonical digest and every image hash under root."""
    if manifest is None:
        manifest = json.loads((Path(root) / "manifest.json").read_text())
    if manifest.get("schema") != "ra-vision-dataset-v1" or signature(manifest) != digest:
        raise ValueError("dataset manifest digest mismatch")
    classes = manifest["classes"]
    for item in manifest["items"]:
        if item["split"] not in {"train", "holdout"} or not 0 <= item["label"] < len(classes):
            raise ValueError("invalid manifest item")
        if file_sha256(Path(root) / item["path"]) != item["sha256"]:
            raise ValueError("dataset file hash mismatch: " + item["path"])
    return manifest


def verified_bytes(path, digest, size):
    with open(path, "rb") as file:
        data = file.read(size + 1)
    if len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("bound checkpoint/input size or digest mismatch")
    return data


def require_readonly(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        if exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}:
            return True
        raise
    os.close(fd)
    raise RuntimeError("training input unexpectedly writable")


def init_descriptor(model, seed, classes, state_digest):
    """Bounded JSON stand-in for the seeded initial weights (registered as the model digest)."""
    return {
        "schema": "ra-vision-init-v1",
        "model": model,
        "seed": seed,
        "classes": classes,
        "state_digest": state_digest,
    }


def bound_inputs(args, binding, checkpoint_path, input_path):
    """Verify the operator-bound init descriptor and dataset manifest against this command."""
    raw_init = verified_bytes(
        checkpoint_path, binding["checkpoint_digest"], binding["checkpoint_size_bytes"]
    )
    raw_manifest = verified_bytes(input_path, binding["input_digest"], binding["input_size_bytes"])
    readonly = require_readonly(checkpoint_path), require_readonly(input_path)
    init = json.loads(raw_init)
    expected = init_descriptor(
        args.model, args.seed, init.get("classes"), args.initial_checkpoint_digest
    )
    if init != expected:
        raise ValueError("bound initial checkpoint differs from the command")
    manifest = json.loads(raw_manifest)
    if len(manifest["classes"]) != init["classes"]:
        raise ValueError("bound class count differs from the dataset")
    return manifest, readonly


def checkpoint_digest(model):
    """Digest of parameters and buffers in key order (name, dtype, shape, raw bytes)."""
    h = hashlib.sha256()
    for name, tensor in sorted(model.state_dict().items()):
        value = tensor.detach().to("cpu").contiguous()
        h.update(name.encode() + b"\0" + str(value.dtype).encode() + b"\0")
        h.update(json.dumps(list(value.shape)).encode() + b"\0")
        h.update(value.numpy().tobytes())
    return "sha256:" + h.hexdigest()


def parameter_digest(model):
    """Digest of trainable parameters only (DDP keeps these identical on every rank)."""
    h = hashlib.sha256()
    for name, tensor in sorted(model.named_parameters()):
        value = tensor.detach().to("cpu").contiguous()
        h.update(name.encode() + b"\0" + str(value.dtype).encode() + b"\0")
        h.update(json.dumps(list(value.shape)).encode() + b"\0")
        h.update(value.numpy().tobytes())
    return "sha256:" + h.hexdigest()


def build_model(name, classes, seed, initial_state=None):
    """Seeded architecture; a pinned state file makes the start identical across runtimes."""
    import torch
    import torchvision

    if name not in MODELS:
        raise ValueError("unsupported model")
    torch.manual_seed(seed)
    model = getattr(torchvision.models, name)(weights=None, num_classes=classes)
    if initial_state:
        state = torch.load(initial_state, map_location="cpu", weights_only=True)
        model.load_state_dict(state, strict=True)
    return model


def preprocess(root, manifest):
    """Deterministic resize-256/center-crop-224/normalize, NCHW float32."""
    import torch
    from PIL import Image
    from torchvision.transforms import functional as F

    tensors = {"train": [], "holdout": []}
    labels = {"train": [], "holdout": []}
    for item in manifest["items"]:
        with Image.open(Path(root) / item["path"]) as image:
            image = image.convert("RGB")
            image = F.center_crop(F.resize(image, 256), 224)
            tensor = F.normalize(F.to_tensor(image), MEAN, STD)
        tensors[item["split"]].append(tensor)
        labels[item["split"]].append(item["label"])
    return {
        split: (torch.stack(tensors[split]), torch.tensor(labels[split], dtype=torch.long))
        for split in tensors
    }


def batch_schedule(train_count, global_batch, steps, seed):
    """Global index batches from seeded epoch permutations (identical on every rank)."""
    import torch

    generator = torch.Generator().manual_seed(seed)
    order, batches = [], []
    for _ in range(steps):
        while len(order) < global_batch:
            order.extend(torch.randperm(train_count, generator=generator).tolist())
        batches.append(order[:global_batch])
        order = order[global_batch:]
    return batches


def validate_environment(args, environ):
    """Refuse before any work when the signed job semantics differ from this command."""
    context = json.loads(environ["RA_CONTEXT_JSON"])
    if context["parameters"]:
        raise RuntimeError("vision training has fixed semantics; parameters are not accepted")
    if context["allocation_mode"] not in {"physical_device", "virtual_slot"}:
        raise RuntimeError("vision training requires a GPU allocation")
    world = int(environ.get("WORLD_SIZE", "1"))
    if (args.strategy == "single") != (world == 1):
        raise RuntimeError("training strategy and WORLD_SIZE disagree")
    local = int(environ.get("LOCAL_WORLD_SIZE", "1"))
    if context["resources"]["accelerator_count"] != local:
        raise RuntimeError("accelerators per node differ from the local rank count")
    if int(environ["RA_WORK_UNITS"]) != args.steps or not 1 <= args.steps <= MAX_STEPS:
        raise ValueError("work units must equal the bounded optimizer step count")
    if args.global_batch % world:
        raise ValueError("global batch must divide evenly across ranks")
    # input_shape carries the per-rank micro batch; equal to the global batch on one device.
    if json.loads(environ["RA_INPUT_SHAPE"]) != [args.global_batch // world, 3, 224, 224]:
        raise ValueError("input shape differs from the fixed per-rank batch")
    if environ["RA_PRECISION"] != "fp32" or int(environ["RA_SEED"]) != args.seed:
        raise ValueError("precision or seed differs from the signed workload")
    if args.strategy == "ddp":
        distributed_node(context, environ, world)
    elif context.get("distributed"):
        raise RuntimeError("distributed context requires --strategy ddp")
    return context, world


def distributed_node(context, environ, world):
    """This Pod's own entry of the signed DistributedTraining (never inferred from rank 0)."""
    d = json.loads(environ["RA_DISTRIBUTED_JSON"])
    if d != context.get("distributed"):
        raise RuntimeError("RA_DISTRIBUTED_JSON differs from the signed context")
    if d["strategy"] != "ddp" or d["launcher"] != "torchrun" or d["world_size"] != world:
        raise RuntimeError("distributed strategy, launcher or world size mismatch")
    if context["allocation_mode"] != "physical_device":
        raise RuntimeError("DISTRIBUTED_SHARED_SLOT_UNSUPPORTED")
    mine = [n for n in d["nodes"] if n["node_ref"] == environ["RA_NODE_NAME"]]
    if len(mine) != 1 or mine[0]["gpus"] != int(environ["LOCAL_WORLD_SIZE"]):
        raise RuntimeError("this node is not exactly one signed distributed member")
    return mine[0]


RANK_KEYS = (
    "node",
    "pod_uid",
    "device_name",
    "initial_checkpoint_digest",
    "output_checkpoint_digest",
    "output_parameter_digest",
)


def gather_ranks(record):
    """Every rank's own observed record (all_gather_object; rank order)."""
    import torch.distributed as dist

    records = [None] * dist.get_world_size()
    dist.all_gather_object(records, record)
    return records


def check_ranks(records, world, local_world=1):
    """One record per rank; every Pod on its own node runs exactly local ranks 0..local_world-1.

    Replicas start equal, end with equal synchronized parameters, and changed.
    """
    records = sorted(records, key=lambda r: r["rank"])
    if [r["rank"] for r in records] != list(range(world)):
        raise RuntimeError("distributed rank records missing or duplicated")
    if any(not r.get(k) for r in records for k in RANK_KEYS):
        raise RuntimeError("distributed rank record lacks observed identity")
    pods = {}
    for r in records:
        pods.setdefault(r["pod_uid"], []).append(r)
    for members in pods.values():
        if len({r["node"] for r in members}) != 1 or sorted(
            r["local_rank"] for r in members
        ) != list(range(local_world)):
            raise RuntimeError("distributed Pod does not hold exactly its local ranks on one node")
    if len(pods) * local_world != world or len({r["node"] for r in records}) != len(pods):
        raise RuntimeError("distributed ranks do not run on distinct Pods and nodes")
    # BatchNorm buffers are rank-local after the last forward; parameters must match.
    if len({(r["initial_checkpoint_digest"], r["output_parameter_digest"]) for r in records}) != 1:
        raise RuntimeError("DDP replicas diverged")
    if records[0]["initial_checkpoint_digest"] == records[0]["output_checkpoint_digest"]:
        raise RuntimeError("training produced no parameter change")
    return records


class StagingPeerFailed(RuntimeError):
    code = "STAGING_PEER_FAILED"


def run_staging(args, stage, barrier, agree=None):
    """stage(args) between a barrier and an agreement: every member starts together and
    compute starts after the slowest. ``agree(ok)`` (an all-reduced MIN under DDP) replaces
    the closing barrier so one member's failure fails every rank instead of hanging them."""
    barrier()
    window_start = time.perf_counter()
    try:
        receipt = stage(args)
        if not isinstance(receipt, dict):
            raise RuntimeError("staging receipt must be a JSON object")
        json.dumps(receipt, allow_nan=False)
    except BaseException:
        if agree is not None:
            agree(False)
        raise
    own = time.perf_counter() - window_start
    if agree is None:
        barrier()
    elif not agree(True):
        raise StagingPeerFailed("another rank failed to stage")
    window = time.perf_counter() - window_start
    return receipt, own, window


def sync_batchnorm(model):
    """Global-batch BatchNorm under DDP (same statistics as one device at the global batch)."""
    import torch

    return torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)


def run(args, environ=os.environ, stage=None):
    context, world = validate_environment(args, environ)
    import torch

    rank = int(environ.get("RANK", "0"))
    local_rank = int(environ.get("LOCAL_RANK", "0"))
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU required; no CPU fallback")
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    import torchvision

    actual = {"pytorch": torch.__version__, "torchvision": torchvision.__version__}
    expected = (
        distributed_node(context, environ, world)["accelerator_model"]
        if args.strategy == "ddp"
        else context["accelerator_model"]
    )
    if arch != context["arch"] or torch.cuda.get_device_name(device) != expected:
        raise RuntimeError("hardware differs from the qualified context")
    if any(actual.get(k) != v for k, v in context["runtime_versions"].items() if k in actual):
        raise RuntimeError("runtime differs from the qualified context")

    if args.strategy == "ddp":
        import torch.distributed as dist

        dist.init_process_group(context["distributed"]["collective"])
    staging = None
    if stage is not None:
        if args.strategy == "ddp":

            def agree(ok):
                flag = torch.tensor([1 if ok else 0], device=device)
                dist.all_reduce(flag, op=dist.ReduceOp.MIN)
                return bool(flag.item())

            staging = run_staging(args, stage, lambda: dist.barrier(device_ids=[local_rank]), agree)
        else:
            staging = run_staging(args, stage, lambda: None)

    prep_start = time.perf_counter()
    binding = json.loads(environ["RA_TRAINING_BINDING"]) if args.isolated else None
    if args.isolated:
        if Path("/ra-source").exists():
            raise RuntimeError("original source is exposed to training container")
        bound, readonly = bound_inputs(args, binding, args.checkpoint_path, args.input_path)
    manifest = load_manifest(args.data, args.dataset_digest, bound if args.isolated else None)
    classes = len(manifest["classes"])
    data = preprocess(args.data, manifest)
    model = build_model(args.model, classes, args.seed, args.initial_state)
    initial = checkpoint_digest(model)
    if initial != args.initial_checkpoint_digest:
        raise ValueError("seeded initial checkpoint differs from the signed model digest")
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    model = model.to(device)
    train_x, train_y = data["train"][0].to(device), data["train"][1].to(device)
    if args.strategy == "ddp":
        model = torch.nn.parallel.DistributedDataParallel(
            sync_batchnorm(model), device_ids=[local_rank]
        )
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    loss_fn = torch.nn.CrossEntropyLoss()
    batches = batch_schedule(len(train_y), args.global_batch, args.steps, args.seed)
    per_rank = args.global_batch // world
    preparation = time.perf_counter() - prep_start

    job, attempt = environ["RA_JOB_ID"], environ["RA_ATTEMPT_ID"]
    losses, step_seconds = [], []
    model.train()
    torch.cuda.synchronize(device)
    torch.cuda.reset_peak_memory_stats(device)
    started = time.perf_counter()
    for step, indices in enumerate(batches, start=1):
        begin = time.perf_counter()
        local = torch.tensor(indices[rank * per_rank : (rank + 1) * per_rank], device=device)
        optimizer.zero_grad(set_to_none=True)
        loss = loss_fn(model(train_x[local]), train_y[local])
        loss.backward()  # DDP all-reduce happens here, inside the timed loop.
        optimizer.step()
        value = float(loss.detach().item())  # Synchronizes this step.
        if not math.isfinite(value):
            raise FloatingPointError(f"non-finite loss at step {step}")
        losses.append(value)
        step_seconds.append(time.perf_counter() - begin)
        if rank == 0:
            emit(
                PROGRESS,
                {
                    "schema": "ra-progress-v1",
                    "job_id": job,
                    "attempt_id": attempt,
                    "epoch": step,
                    "total_epochs": args.steps,
                    "optimizer_steps": step,
                    "loss": value,
                    "elapsed_seconds": time.perf_counter() - started,
                    "observed_at": datetime.now(UTC).isoformat(),
                },
            )
    torch.cuda.synchronize(device)
    elapsed = time.perf_counter() - started
    peak = torch.cuda.max_memory_allocated(device) / 1024**2
    core = model.module if args.strategy == "ddp" else model
    output = checkpoint_digest(core)
    if output == initial:
        raise RuntimeError("training produced no parameter change")
    core.eval()
    with torch.no_grad():
        hx, hy = data["holdout"][0].to(device), data["holdout"][1].to(device)
        logits = core(hx)
        holdout_loss = float(loss_fn(logits, hy).item())
        holdout_top1 = float((logits.argmax(1) == hy).float().mean().item())
    ranks = None
    if args.strategy == "ddp":
        mine = {
            "rank": rank,
            "local_rank": local_rank,
            "node": environ.get("RA_NODE_NAME"),
            "pod_uid": environ.get("RA_POD_UID"),
            "device_name": torch.cuda.get_device_name(device),
            "initial_checkpoint_digest": initial,
            "output_checkpoint_digest": output,
            "output_parameter_digest": parameter_digest(core),
            "loop_seconds": float(elapsed),
            "peak_memory_mib": float(peak),
            "losses_digest": signature(losses),
        }
        if staging is not None:
            mine.update(
                staging_receipt=staging[0],
                staging_seconds=staging[1],
                staging_window_seconds=staging[2],
            )
        ranks = check_ranks(gather_ranks(mine), world, int(environ.get("LOCAL_WORLD_SIZE", "1")))
        torch.distributed.destroy_process_group()
    if rank != 0:
        return None
    if args.output:
        torch.save(core.state_dict(), Path(args.output) / "model.pt")
    if args.isolated:
        verified_bytes(
            args.checkpoint_path, binding["checkpoint_digest"], binding["checkpoint_size_bytes"]
        )
        verified_bytes(args.input_path, binding["input_digest"], binding["input_size_bytes"])
    ordered = sorted(step_seconds)
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
            "peak_memory_mib": float(peak),
            "quality_value": holdout_top1,
            "sample_count": args.steps,
            "work_units": args.steps,
            "latency_p50_ms": ordered[len(ordered) // 2] * 1000,
            "latency_p95_ms": ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
            * 1000,
            "latency_p99_ms": None,
            "throughput": float(args.steps * args.global_batch / elapsed),
            "gpu_utilization": None,
            "power_watts": None,
            "temperature_celsius": None,
        },
        "error_code": None,
    }
    evidence = {
        "schema": "ra-vision-training-evidence-v1",
        "measurement_boundary": BOUNDARY,
        "model_name": args.model,
        "strategy": args.strategy,
        "world_size": world,
        "batchnorm": "sync-global-batch"
        if args.strategy == "ddp"
        else "single-device-global-batch",
        "global_batch": args.global_batch,
        "optimizer": {"name": "AdamW", "lr": args.lr, "weight_decay": args.weight_decay},
        "dataset_manifest_digest": args.dataset_digest,
        "train_images": len(data["train"][1]),
        "holdout_images": len(data["holdout"][1]),
        "initial_checkpoint_digest": initial,
        "output_checkpoint_digest": output,
        "losses": losses,
        "holdout_loss": holdout_loss,
        "holdout_top1": holdout_top1,
        "preparation_seconds": preparation,
        "device_name": torch.cuda.get_device_name(device),
        "runtime": actual,
        "node": environ.get("RA_NODE_NAME"),
        "pod_uid": environ.get("RA_POD_UID"),
    }
    if ranks is not None:
        evidence["ranks"] = ranks
    if staging is not None:
        evidence.update(
            staging_receipt=staging[0],
            staging_seconds=staging[1],
            staging_window_seconds=max(r["staging_window_seconds"] for r in ranks)
            if ranks
            else staging[2],
        )
    envelope = {"result": result, "digest": signature(result), "training_evidence": evidence}
    if args.isolated:
        output_checkpoint = {
            "schema": "ra-vision-output-v1",
            "model": args.model,
            "initial_state_digest": initial,
            "output_state_digest": output,
            "optimizer_steps": args.steps,
            "global_batch": args.global_batch,
            "losses": [round(v, 6) for v in losses],
            "holdout_top1": holdout_top1,
            "holdout_loss": round(holdout_loss, 6),
        }
        envelope["training_receipt"] = {
            "job_id": job,
            "attempt_id": attempt,
            "result_digest": signature(result),
            "initial_checkpoint_digest": binding["checkpoint_digest"],
            "input_digest": binding["input_digest"],
            "checkpoint_after_digest": binding["checkpoint_digest"],
            "input_after_digest": binding["input_digest"],
            "checkpoint_readonly": readonly[0],
            "input_readonly": readonly[1],
            "source_not_mounted": True,
            "output_checkpoint_digest": signature(output_checkpoint),
            "output_checkpoint": output_checkpoint,
        }
    emit(RESULT, envelope)
    return evidence


def parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", choices=MODELS, required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--dataset-digest", required=True)
    p.add_argument("--initial-checkpoint-digest", required=True)
    p.add_argument("--global-batch", type=int, required=True)
    p.add_argument("--steps", type=int, required=True)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--weight-decay", type=float, default=0.01)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--strategy", choices=("single", "ddp"), default="single")
    p.add_argument("--output")
    p.add_argument("--isolated", action="store_true", help="read the RA training binding inputs")
    p.add_argument("--checkpoint-path", default="/ra-checkpoint/checkpoint.json")
    p.add_argument("--input-path", default="/ra-input/input.json")
    p.add_argument("--initial-state", help="pinned state_dict file; digest must still match")
    p.add_argument("--print-initial-digest", action="store_true", help="CPU only; no training")
    p.add_argument("--write-initial-state", help="with --print-initial-digest: save the state")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.print_initial_digest:
        manifest = json.loads((Path(args.data) / "manifest.json").read_text())
        model = build_model(args.model, len(manifest["classes"]), args.seed, args.initial_state)
        if args.write_initial_state:
            import torch

            torch.save(model.state_dict(), args.write_initial_state)
        print(checkpoint_digest(model))
        return 0
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
