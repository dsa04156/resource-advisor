"""Joint GPU-student / Mobilint-teacher knowledge-distillation training on one device.

The GPU student (vision_training's seeded torchvision model, data and schedule) is
the only model whose weights change: every optimizer step sends its normalized
batch to the live Mobilint ARIES teacher (frozen ResNet50 MXQ forward) and trains
on CE + T^2 * KL against the teacher logits of the manifest's ImageNet classes.

The KD objective is signed: ``--kd-config-digest`` must equal the digest of the
fixed objective (CE+KL, T=4, alpha=0.5) and the pinned teacher model/runtime, and
the workload registers the same value as its config digest, so vanilla and KD
profiles never mix. The teacher URL and Pod UID only locate the pinned teacher:
its identity is checked before any work and on every response. One device only;
a world size other than 1 fails closed.

Timing: the GPU step wall time includes the teacher RPC; the client RPC time and
the teacher's own server/inference times are reported beside it, never added.
"""

import json
import math
import os
import platform
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

try:
    from . import mobilint_teacher as mt
    from . import vision_training as vt
except ImportError:  # files copied side by side into an image and run with python -I
    import mobilint_teacher as mt
    import vision_training as vt

BOUNDARY = "vision-kd-mobilint-teacher-fixed-steps-v1"
TEMPERATURE = 4.0
ALPHA = 0.5
TEACHER_SCHEMA = "ra-kd-teacher-summary-v1"


def kd_config(teacher_model_sha256, teacher_runtime_version):
    """The signed KD objective; its digest is the workload's config digest."""
    return {
        "schema": "ra-kd-config-v1",
        "objective": "ce+kl",
        "loss": "(1 - alpha) * CE(student, label) + alpha * T^2 * KL(teacher_T || student_T)",
        "temperature": TEMPERATURE,
        "alpha": ALPHA,
        "class_mapping": "manifest.classes (ImageNet indices, label order)",
        "teacher": {
            "role": "frozen-teacher-forward",
            "model_file": "resnet50_IMAGENET1K_V1.mxq",
            "model_sha256": teacher_model_sha256,
            "runtime": "qbruntime",
            "runtime_version": teacher_runtime_version,
            "input": "float32 NHWC 224x224x3, ImageNet mean/std normalized",
        },
    }


def kd_config_digest(teacher_model_sha256, teacher_runtime_version):
    return vt.signature(kd_config(teacher_model_sha256, teacher_runtime_version))


def teacher_pin(args):
    return {
        "model_sha256": args.teacher_model_sha256,
        "runtime_version": args.teacher_runtime_version,
        "pod_uid": args.teacher_pod_uid,
    }


def preflight(args, environ):
    """Refuse before loading data or a GPU when semantics or the teacher differ."""
    if args.strategy != "single" or int(environ.get("WORLD_SIZE", "1")) != 1:
        raise RuntimeError("joint KD training runs on one device only")
    expected = kd_config_digest(args.teacher_model_sha256, args.teacher_runtime_version)
    if args.kd_config_digest != expected:
        raise ValueError("KD config digest differs from the signed objective and teacher")
    context, world = vt.validate_environment(args, environ)
    with urllib.request.urlopen(args.teacher_url + "/v1/teacher/health", timeout=10) as r:
        health = json.loads(r.read())
    identity = health.get("teacher") or {}
    mismatch = sorted(k for k, v in teacher_pin(args).items() if identity.get(k) != v)
    if not health.get("ready") or mismatch:
        raise RuntimeError(f"teacher is not the pinned teacher: {mismatch or 'not ready'}")
    return context, world, identity


def class_indices(manifest):
    classes = manifest["classes"]
    if (
        not classes
        or len(set(classes)) != len(classes)
        or not all(isinstance(c, int) and 0 <= c < mt.CLASSES for c in classes)
    ):
        raise ValueError("manifest classes must be distinct ImageNet indices")
    return classes


def run(args, environ=os.environ, stage=None):
    """``stage`` (optional) stages the dataset before preparation, like vision_training."""
    context, _, identity = preflight(args, environ)
    import torch
    import torch.nn.functional as F

    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU required; no CPU fallback")
    device = torch.device("cuda", 0)
    torch.cuda.set_device(device)
    import torchvision

    actual = {"pytorch": torch.__version__, "torchvision": torchvision.__version__}
    if (
        arch != context["arch"]
        or torch.cuda.get_device_name(device) != context["accelerator_model"]
    ):
        raise RuntimeError("hardware differs from the qualified context")
    if any(actual.get(k) != v for k, v in context["runtime_versions"].items() if k in actual):
        raise RuntimeError("runtime differs from the qualified context")

    # Staging precedes preparation; the teacher RPC stays inside the timed loop.
    staging = vt.run_staging(args, stage, lambda: None) if stage is not None else None
    prep_start = time.perf_counter()
    binding = json.loads(environ["RA_TRAINING_BINDING"]) if args.isolated else None
    bound = None
    if args.isolated:
        if Path("/ra-source").exists():
            raise RuntimeError("original source is exposed to training container")
        bound, readonly = vt.bound_inputs(args, binding, args.checkpoint_path, args.input_path)
    manifest = vt.load_manifest(args.data, args.dataset_digest, bound)
    classes = class_indices(manifest)
    data = vt.preprocess(args.data, manifest)
    model = vt.build_model(args.model, len(classes), args.seed, args.initial_state)
    initial = vt.checkpoint_digest(model)
    if initial != args.initial_checkpoint_digest:
        raise ValueError("seeded initial checkpoint differs from the signed model digest")
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    model = model.to(device)
    train_x, train_y = data["train"][0].to(device), data["train"][1].to(device)
    selected = torch.tensor(classes, dtype=torch.long)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    batches = vt.batch_schedule(len(train_y), args.global_batch, args.steps, args.seed)
    preparation = time.perf_counter() - prep_start
    vt.record_preparation(staging, preparation)

    job, attempt = environ["RA_JOB_ID"], environ["RA_ATTEMPT_ID"]
    pin = teacher_pin(args)
    losses, ce_losses, kd_losses, step_seconds, rpc_seconds = [], [], [], [], []
    request_bytes = response_bytes = 0
    server_infer = server_handler = 0.0
    model.train()
    torch.cuda.synchronize(device)
    torch.cuda.reset_peak_memory_stats(device)
    started = time.perf_counter()
    for step, indices in enumerate(batches, start=1):
        begin = time.perf_counter()
        local = torch.tensor(indices, device=device)
        x, y = train_x[local], train_y[local]
        nhwc = x.permute(0, 2, 3, 1).contiguous().cpu().numpy()
        logits, receipt = mt.teacher_logits(args.teacher_url, nhwc, timeout=30.0, expect=pin)
        if receipt["teacher"] != identity:
            raise RuntimeError("teacher identity changed during training")
        rpc_seconds.append(receipt["client"]["elapsed_seconds"])
        request_bytes += receipt["client"]["request_bytes"]
        response_bytes += receipt["client"]["response_bytes"]
        server_infer += receipt["server"]["infer_seconds"]
        server_handler += receipt["server"]["handler_seconds"]
        teacher = torch.from_numpy(logits).index_select(1, selected).to(device)
        optimizer.zero_grad(set_to_none=True)
        student = model(x)
        ce = F.cross_entropy(student, y)
        kd = F.kl_div(
            F.log_softmax(student / TEMPERATURE, dim=1),
            F.softmax(teacher / TEMPERATURE, dim=1),
            reduction="batchmean",
        ) * (TEMPERATURE**2)
        loss = (1 - ALPHA) * ce + ALPHA * kd
        loss.backward()
        optimizer.step()
        value = float(loss.detach().item())  # Synchronizes this step.
        if not math.isfinite(value):
            raise FloatingPointError(f"non-finite loss at step {step}")
        losses.append(value)
        ce_losses.append(float(ce.detach().item()))
        kd_losses.append(float(kd.detach().item()))
        step_seconds.append(time.perf_counter() - begin)
        vt.emit(
            vt.PROGRESS,
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
    output = vt.checkpoint_digest(model)
    if output == initial:
        raise RuntimeError("training produced no parameter change")
    model.eval()
    with torch.no_grad():
        hx, hy = data["holdout"][0].to(device), data["holdout"][1].to(device)
        holdout_logits = model(hx)
        holdout_loss = float(F.cross_entropy(holdout_logits, hy).item())
        holdout_top1 = float((holdout_logits.argmax(1) == hy).float().mean().item())
    if args.output:
        torch.save(model.state_dict(), Path(args.output) / "model.pt")
    if args.isolated:
        vt.verified_bytes(
            args.checkpoint_path, binding["checkpoint_digest"], binding["checkpoint_size_bytes"]
        )
        vt.verified_bytes(args.input_path, binding["input_digest"], binding["input_size_bytes"])
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
    summary = {
        "schema": TEACHER_SCHEMA,
        "role": "frozen-teacher-forward",
        "device_class": "npu",
        "device_name": identity.get("device_name"),
        "accelerator_model": "ARIES2",
        "model_sha256": identity.get("model_sha256"),
        "model_file": identity.get("model_file"),
        "runtime": identity.get("runtime"),
        "runtime_version": identity.get("runtime_version"),
        "target_cores": identity.get("target_cores"),
        "node_name": identity.get("node_name"),
        "pod_uid": identity.get("pod_uid"),
        "reservation": {
            "resource": "mobilint.com/npu",
            "count": 1,
            "mode": "pre-reserved auxiliary Pod",
        },
        "calls": len(rpc_seconds),
        "request_bytes": request_bytes,
        "response_bytes": response_bytes,
        "client_rpc_seconds": sum(rpc_seconds),
        "server_infer_seconds": server_infer,
        "server_handler_seconds": server_handler,
        "objective": {"loss": "ce+kl", "temperature": TEMPERATURE, "alpha": ALPHA},
        "timing": "client_rpc_seconds is inside the GPU training wall time; never add it again",
    }
    evidence = {
        "schema": "ra-vision-kd-training-evidence-v1",
        "measurement_boundary": BOUNDARY,
        "model_name": args.model,
        "strategy": "single",
        "world_size": 1,
        "global_batch": args.global_batch,
        "optimizer": {"name": "AdamW", "lr": args.lr, "weight_decay": args.weight_decay},
        "kd_config_digest": args.kd_config_digest,
        "kd_config": kd_config(args.teacher_model_sha256, args.teacher_runtime_version),
        "dataset_manifest_digest": args.dataset_digest,
        "class_indices": classes,
        "train_images": len(data["train"][1]),
        "holdout_images": len(data["holdout"][1]),
        "initial_checkpoint_digest": initial,
        "output_checkpoint_digest": output,
        "losses": losses,
        "ce_losses": ce_losses,
        "kd_losses": kd_losses,
        "rpc_seconds": rpc_seconds,
        "step_seconds": step_seconds,
        "holdout_loss": holdout_loss,
        "holdout_top1": holdout_top1,
        "preparation_seconds": preparation,
        "device_name": torch.cuda.get_device_name(device),
        "runtime": actual,
        "node": environ.get("RA_NODE_NAME"),
        "pod_uid": environ.get("RA_POD_UID"),
        "teacher": summary,
    }
    if staging is not None:
        evidence.update(
            staging_receipt=staging[0],
            staging_seconds=staging[1],
            staging_window_seconds=staging[2],
        )
    envelope = {"result": result, "digest": vt.signature(result), "training_evidence": evidence}
    if args.isolated:
        output_checkpoint = {
            "schema": "ra-vision-kd-output-v1",
            "model": args.model,
            "initial_state_digest": initial,
            "output_state_digest": output,
            "initial_weights_digest": initial,
            "final_weights_digest": output,
            "optimizer_steps": args.steps,
            "global_batch": args.global_batch,
            "kd_config_digest": args.kd_config_digest,
            "losses": [round(v, 6) for v in losses],
            "holdout_top1": holdout_top1,
            "holdout_loss": round(holdout_loss, 6),
            "teacher": summary,
        }
        envelope["training_receipt"] = {
            "job_id": job,
            "attempt_id": attempt,
            "result_digest": vt.signature(result),
            "initial_checkpoint_digest": binding["checkpoint_digest"],
            "input_digest": binding["input_digest"],
            "checkpoint_after_digest": binding["checkpoint_digest"],
            "input_after_digest": binding["input_digest"],
            "checkpoint_readonly": readonly[0],
            "input_readonly": readonly[1],
            "source_not_mounted": True,
            "output_checkpoint_digest": vt.signature(output_checkpoint),
            "output_checkpoint": output_checkpoint,
        }
    vt.emit(vt.RESULT, envelope)
    return evidence


def parser():
    p = vt.parser()
    p.description = __doc__.splitlines()[0]
    p.add_argument("--teacher-url", required=True)
    p.add_argument("--teacher-model-sha256", required=True)
    p.add_argument("--teacher-runtime-version", required=True)
    p.add_argument("--teacher-pod-uid", required=True)
    p.add_argument(
        "--kd-config-digest", help="signed KD objective digest (see --print-kd-config-digest)"
    )
    p.add_argument("--print-kd-config-digest", action="store_true", help="no training")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.print_kd_config_digest:
        print(kd_config_digest(args.teacher_model_sha256, args.teacher_runtime_version))
        return 0
    if args.print_initial_digest:
        raise SystemExit(
            "the seeded student equals vanilla: use vision_training --print-initial-digest"
        )
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
