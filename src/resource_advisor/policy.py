"""Pure compatibility checks: inventory is not model qualification."""

from datetime import datetime

from .contracts import Candidate, CapabilitySnapshot, RuntimeVariant, WorkloadSpec, now, signature


def context_signature(candidate: Candidate, variant: RuntimeVariant) -> str:
    value = {
        "context": candidate.context.model_dump(mode="json"),
        "image": variant.image,
        "compiled_artifact_digest": variant.compiled_artifact_digest,
        "command": variant.command,
        "pilot_command": variant.pilot_command,
        "backend": candidate.backend,
    }
    if variant.thermal_policy is not None:
        value["thermal_policy"] = variant.thermal_policy.model_dump(mode="json")
    if variant.load_context_policy is not None:
        value["load_context_policy"] = variant.load_context_policy
    if variant.kubernetes_runtime_bundle_ref is not None:
        value["kubernetes_runtime_bundle_ref"] = variant.kubernetes_runtime_bundle_ref
    if variant.distributed_launch is not None:
        value["distributed_launch"] = variant.distributed_launch
    return signature(value)


# Per-node evidence must match the candidate's own qualified capability exactly.
NODE_IDENTITY = (
    "backend",
    "backend_cluster_id",
    "arch",
    "accelerator_vendor",
    "accelerator_model",
    "device_class",
    "allocation_mode",
    "resource_key",
    "runtime_versions",
    "environment_digest",
    "memory_model",
    "power_mode",
)
# GPU model may differ per node, but only when each node's model is separately qualified.
SHARED_IDENTITY = tuple(k for k in NODE_IDENTITY if k != "accelerator_model")


def distributed_reasons(spec, candidate, variant, cap, node_caps, at):
    """Distributed launch checks; ``node_caps`` maps capability_ref -> snapshot.

    Every allocated node is validated against its own fresh capability, which must
    be identical hardware/runtime to the candidate's; shared slots never count as
    GPUs. Without per-node evidence the candidate fails closed.
    """
    ctx, d = candidate.context, candidate.context.distributed
    if d is None:
        return ["DISTRIBUTED_LAUNCH_MISMATCH"] if variant.distributed_launch else []
    i = spec.identity
    errors = {
        "DISTRIBUTED_LAUNCH_MISMATCH": variant.distributed_launch != "torchrun-ddp",
        "DISTRIBUTED_SLURM_UNSUPPORTED": candidate.backend == "slurm",
        "DISTRIBUTED_TRAINING_ONLY": i.task_type != "training",
        "DISTRIBUTED_GLOBAL_BATCH_MISMATCH": i.global_batch_size
        != i.batch_size * d.world_size * i.gradient_accumulation,
        "DISTRIBUTED_SHARED_SLOT_UNSUPPORTED": ctx.allocation_mode != "physical_device"
        or cap.allocation_mode != "physical_device",
        "DISTRIBUTED_COLLECTIVE_UNSUPPORTED": cap.device_class != "gpu"
        or (d.collective == "nccl" and cap.accelerator_vendor != "nvidia"),
        "DISTRIBUTED_CANDIDATE_NODE_OUTSIDE_ALLOCATION": (cap.node_ref, cap.ref)
        not in {(n.node_ref, n.capability_ref) for n in d.nodes},
    }
    reasons = [reason for reason, failed in errors.items() if failed]
    if node_caps is None:
        return reasons + ["DISTRIBUTED_NODE_EVIDENCE_REQUIRED"]
    for node in d.nodes:
        member = node_caps.get(node.capability_ref)
        if member is None:
            reasons.append("DISTRIBUTED_NODE_EVIDENCE_MISSING")
            continue
        age = ((at or now()) - member.observed_at).total_seconds()
        if member.node_ref != node.node_ref or member.accelerator_model != node.accelerator_model:
            reasons.append("DISTRIBUTED_NODE_CAPABILITY_MISMATCH")
        elif any(getattr(member, k) != getattr(cap, k) for k in SHARED_IDENTITY):
            # One Job: same cluster route, image, runtime ABI and allocation kind.
            reasons.append("DISTRIBUTED_NODE_HETEROGENEOUS")
        elif node.validation_ref not in variant.validation_refs or (
            node.node_ref,
            node.validation_ref,
        ) not in (getattr(node_caps, "qualified", None) or ()):
            # The reference must name this node's native training proof, not just a string.
            reasons.append("DISTRIBUTED_NODE_MODEL_UNQUALIFIED")
        elif age < -5 or age > member.valid_for_seconds:
            reasons.append("DISTRIBUTED_NODE_STALE")
        elif not member.ready:
            reasons.append("DISTRIBUTED_NODE_UNAVAILABLE")
        elif member.allocatable_count < node.gpus or not member.reservation_verified:
            reasons.append("DISTRIBUTED_NODE_INSUFFICIENT_GPUS")
        elif (
            ctx.resources.host_cpu > member.host_cpu
            or ctx.resources.host_memory_mib > member.host_memory_mib
        ):
            reasons.append("DISTRIBUTED_NODE_INSUFFICIENT_HOST")
    return sorted(set(reasons), key=reasons.index)


def compatibility(
    spec: WorkloadSpec,
    candidate: Candidate,
    variant: RuntimeVariant,
    cap: CapabilitySnapshot,
    at: datetime | None = None,
    node_caps: dict | None = None,
) -> list[str]:
    errors = []
    ctx = candidate.context
    age = ((at or now()) - cap.observed_at).total_seconds()
    checks = {
        "CAPABILITY_STALE": age < -5 or age > cap.valid_for_seconds,
        "RUNTIME_FINGERPRINT_INCOMPLETE": (
            ctx.runtime_versions.get("profile_reuse", "").startswith("blocked-")
            or (
                cap.device_class == "npu"
                and variant.accelerator_vendor in {"intel", "mobilint", "rockchip"}
                and not ctx.runtime_versions.get("firmware_identity")
            )
        ),
        "NODE_UNAVAILABLE": not cap.ready,
        "WRONG_PROJECT_OR_WORKLOAD": (
            variant.project_ref != spec.project_ref or variant.workload_ref != spec.ref
        ),
        "WRONG_REFERENCE": candidate.variant_ref != variant.ref
        or candidate.capability_ref != cap.ref,
        "BACKEND_MISMATCH": candidate.backend != cap.backend,
        "KUBERNETES_RUNTIME_BINDING_ON_SLURM": candidate.backend == "slurm"
        and variant.kubernetes_runtime_bundle_ref is not None,
        "ARCH_MISMATCH": not (variant.arch == ctx.arch == cap.arch),
        "VENDOR_MISMATCH": variant.accelerator_vendor != cap.accelerator_vendor,
        "DEVICE_CLASS_MISMATCH": variant.device_class != cap.device_class,
        "MODEL_MISMATCH": variant.model_digest != spec.identity.model_digest,
        "LOGICAL_WORKLOAD_MISMATCH": variant.workload_signature != signature(spec.identity),
        "PRECISION_MISMATCH": variant.precision != spec.identity.precision,
        "INPUT_SHAPE_UNVERIFIED": spec.identity.input_shape not in variant.supported_shapes,
        "ENVIRONMENT_MISMATCH": not (
            variant.environment_digest == ctx.environment_digest == cap.environment_digest
        ),
        "RUNTIME_MISMATCH": not (
            variant.runtime_versions == ctx.runtime_versions == cap.runtime_versions
        ),
        "DEVICE_CONTEXT_MISMATCH": any(
            [
                ctx.accelerator_model != cap.accelerator_model,
                ctx.memory_model != cap.memory_model,
                ctx.power_mode != cap.power_mode,
                ctx.allocation_mode != cap.allocation_mode,
            ]
        ),
        "INSUFFICIENT_CAPACITY": (
            ctx.resources.accelerator_count > cap.allocatable_count
            or ctx.resources.host_cpu > cap.host_cpu
            or ctx.resources.host_memory_mib > cap.host_memory_mib
        ),
        "RESERVATION_UNVERIFIED": ctx.resources.accelerator_count > 0
        and not cap.reservation_verified,
        "CPU_DEVICE_COUNT": cap.device_class == "cpu" and ctx.resources.accelerator_count != 0,
        "ACCELERATOR_DEVICE_COUNT": cap.device_class != "cpu"
        and ctx.resources.accelerator_count == 0,
        "MODEL_UNVERIFIED": variant.verification not in {"MODEL_VERIFIED", "TRAINING_VERIFIED"},
        "TRAINING_UNVERIFIED": spec.identity.task_type == "training"
        and variant.verification != "TRAINING_VERIFIED",
        "CONTAINER_IMAGE_MISSING": candidate.backend == "kubernetes" and not variant.image,
    }
    for reason, failed in checks.items():
        if failed:
            errors.append(reason)
    return errors + distributed_reasons(spec, candidate, variant, cap, node_caps, at)


def execution_compatibility(spec, candidate, variant, cap, *, operational=False, node_caps=None):
    """Manual lab runs may use older observations; never forge renewed evidence.

    Distributed per-node staleness is never relaxed: every node needs fresh evidence.
    """
    errors = compatibility(spec, candidate, variant, cap, node_caps=node_caps)
    if operational and cap.observed_at <= now() and candidate.context.distributed is None:
        errors = [
            reason
            for reason in errors
            if reason not in {"CAPABILITY_STALE", "RUNTIME_FINGERPRINT_INCOMPLETE"}
        ]
    return errors
