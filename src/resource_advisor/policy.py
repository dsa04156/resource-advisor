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
    return signature(value)


def compatibility(
    spec: WorkloadSpec,
    candidate: Candidate,
    variant: RuntimeVariant,
    cap: CapabilitySnapshot,
    at: datetime | None = None,
) -> list[str]:
    errors = []
    ctx = candidate.context
    age = ((at or now()) - cap.observed_at).total_seconds()
    checks = {
        "CAPABILITY_STALE": age < -5 or age > cap.valid_for_seconds,
        "NODE_UNAVAILABLE": not cap.ready,
        "WRONG_PROJECT_OR_WORKLOAD": (
            variant.project_ref != spec.project_ref or variant.workload_ref != spec.ref
        ),
        "WRONG_REFERENCE": candidate.variant_ref != variant.ref
        or candidate.capability_ref != cap.ref,
        "BACKEND_MISMATCH": candidate.backend != cap.backend,
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
    return errors


def execution_compatibility(spec, candidate, variant, cap, *, operational=False):
    """Manual lab runs may use older observations; never forge renewed evidence."""
    errors = compatibility(spec, candidate, variant, cap)
    if operational and cap.observed_at <= now():
        errors = [reason for reason in errors if reason != "CAPABILITY_STALE"]
    return errors
