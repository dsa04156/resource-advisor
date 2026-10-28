"""Registry contracts for the live CPU training runner. Builds, never registers.

Each distinct setting (seed, width, learning rate, epochs) is its own workload,
checkpoint and isolation binding, so historical qualified identities are never
modified. CPU execution is explicit: zero accelerators, no GPU fallback.
"""

from pathlib import Path

from . import live_training
from .contracts import (
    Candidate,
    CapabilitySnapshot,
    ExecutionContext,
    ExecutionPolicy,
    QualityPolicy,
    Resources,
    RuntimeVariant,
    WorkloadIdentity,
    WorkloadSpec,
    signature,
)
from .training import TrainingIsolation

ACCELERATOR_MODEL = "none (CPU-only training)"


def runner_digest():
    return "sha256:" + __import__("hashlib").sha256(Path(live_training.__file__).read_bytes()).hexdigest()


def build(
    *,
    project,
    ref,
    backend,
    cluster,
    node,
    observed_at,
    valid_for_seconds,
    capability_evidence,
    validation_refs,
    setting,
    resources,
    node_cpu,
    node_memory_mib,
    arch,
    environment_digest,
    runtime_versions,
    command,
    image=None,
    host_cpu_model=None,
    execution=None,
):
    checkpoint, inputs = live_training.fixture(
        seed=setting["seed"],
        samples=setting["samples"],
        features=setting["features"],
        hidden=setting["hidden"],
        lr=setting["lr"],
        batch_size=setting["batch_size"],
    )
    cp, inp = live_training.encode(checkpoint), live_training.encode(inputs)
    quality = QualityPolicy(
        metric="training_r2", minimum=setting.get("quality_minimum", 0.0),
        minimum_repeats=1, maximum_peak_memory_mib=float(resources["host_memory_mib"]),
    )
    identity = WorkloadIdentity(
        code_digest=runner_digest(),
        model_digest=signature(checkpoint),
        dataset_version=signature(inputs),
        config_digest=signature(setting),
        task_type="training",
        input_shape=(setting["samples"], setting["features"]),
        batch_size=setting["batch_size"],
        precision="fp64",
        work_units=setting["epochs"],
        quality_contract_digest=signature(quality),
        measurement_boundary=live_training.BOUNDARY,
        model_name="tanh-mlp-v1",
        optimizer="sgd",
        optimizer_parameters={"lr": setting["lr"], "batch_size": setting["batch_size"]},
    )
    context = ExecutionContext(
        arch=arch,
        environment_digest=environment_digest,
        runtime_versions=runtime_versions,
        accelerator_model=ACCELERATOR_MODEL,
        memory_model="host",
        power_mode="default",
        allocation_mode="cpu_only",
        resources=Resources(**resources, accelerator_count=0),
        host_cpu_model=host_cpu_model,
    )
    capability = CapabilitySnapshot(
        ref=ref + "-cap",
        node_ref=node,
        backend_cluster_id=cluster,
        backend=backend,
        observed_at=observed_at,
        valid_for_seconds=valid_for_seconds,
        ready=True,
        arch=arch,
        accelerator_vendor="none",
        accelerator_model=ACCELERATOR_MODEL,
        device_class="cpu",
        allocation_mode="cpu_only",
        allocatable_count=0,
        host_cpu=node_cpu,
        host_memory_mib=node_memory_mib,
        resource_key=None,
        runtime_versions=runtime_versions,
        environment_digest=environment_digest,
        memory_model="host",
        power_mode="default",
        evidence_refs=tuple(capability_evidence),
    )
    variant = RuntimeVariant(
        ref=ref + "-variant",
        workload_ref=ref,
        project_ref=project,
        workload_signature=signature(identity),
        arch=arch,
        accelerator_vendor="none",
        device_class="cpu",
        precision="fp64",
        model_digest=signature(checkpoint),
        image=image,
        environment_digest=environment_digest,
        command=tuple(command),
        verification="TRAINING_VERIFIED",
        validation_refs=tuple(validation_refs),
        supported_shapes=((setting["samples"], setting["features"]),),
        runtime_versions=runtime_versions,
    )
    candidate = Candidate(
        ref="cpu", variant_ref=variant.ref, capability_ref=capability.ref,
        backend=backend, context=context,
    )
    spec = WorkloadSpec(
        ref=ref,
        project_ref=project,
        identity=identity,
        candidates=(candidate,),
        baseline_candidate_ref="cpu",
        quality=quality,
        execution=ExecutionPolicy(**(execution or {})),
        profiling={"checkpoint_digest": signature(checkpoint)},
    )
    binding = TrainingIsolation(
        ref=ref,
        project_ref=project,
        workload_ref=ref,
        workload_digest=signature(spec),
        checkpoint_digest=signature(checkpoint),
        input_digest=signature(inputs),
        checkpoint_size_bytes=len(cp),
        input_size_bytes=len(inp),
        validation_refs=tuple(validation_refs),
        backend=backend,
        device_class="cpu",
    )
    return {
        "workload": spec,
        "variant": variant,
        "capability": capability,
        "training_isolation": binding,
        "files": {"checkpoint.json": cp, "input.json": inp},
    }
