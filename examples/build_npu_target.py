"""Render an NPU binding from archived hardware qualification.

The operator must separately verify native device reservation, UID and image
digest. This renderer neither contacts hardware nor certifies task accuracy.
"""

import math

from resource_advisor.contracts import (
    Candidate,
    CapabilitySnapshot,
    ExecutionContext,
    ExecutionPolicy,
    QualityPolicy,
    Resources,
    RuntimeVariant,
    WorkloadIdentity,
    WorkloadSpec,
    now,
    signature,
)
from resource_advisor.npu_probe import BOUNDARY, ROUNDS, SEED, npu_execution_devices


def build(report, *, ref, project, node, cluster, image, evidence_ref):
    runtime = report["runtime"]
    scope = {
        "mobilint-candy": ("mobilint", "mobilint.com/npu", (224, 344, 3)),
        "intel-conv": ("intel", "npu.intel.com/accel", (1, 3, 16, 16)),
        "rockchip-resnet18": ("rockchip", "rockchip.com/rk3399pro", (224, 224, 3)),
    }
    if runtime not in scope:
        raise ValueError("unqualified NPU runtime")
    vendor, key, shape = scope[runtime]
    samples = report["samples_seconds"]
    if (
        report["evidence_kind"] != "hardware"
        or report["kind"] != "npu-runtime-qualification"
        or report["arch"] != ("arm64" if vendor == "rockchip" else "amd64")
        or tuple(report["input_shape"]) != shape
        or report["work_units"] != ROUNDS
        or report["seed"] != SEED
        or report["warmup"] != 1
        or report["measurement_boundary"] != BOUNDARY
        or len(samples) != ROUNDS
        or any(not math.isfinite(t) or t <= 0 for t in samples)
        or not math.isclose(report["elapsed_seconds"], sum(samples), abs_tol=1e-9)
        or report["quality_value"] != 1
        or not 0 < report["host_process_peak_rss_mib"] <= 1024
    ):
        raise ValueError("matching positive hardware qualification required")
    if runtime == "mobilint-candy" and (
        len(report["output_digests"]) != ROUNDS
        or any("sha256:" + d != report["reference_output_digest"] for d in report["output_digests"])
    ):
        raise ValueError("Mobilint repeat output evidence differs")
    if runtime == "intel-conv":
        # Recalculate errors from raw outputs, rather than trusting quality_value.
        try:
            npu_execution_devices(report["execution_devices"])
            reference = report["reference_output"]
            outputs = report["outputs"]
            if len(reference) != 1 or len(reference[0]) != 4 or len(outputs) != ROUNDS:
                raise ValueError("invalid numerical shape")
            if any(not math.isfinite(x) for x in reference[0]):
                raise ValueError("invalid reference")
            errors = []
            for output in outputs:
                if (
                    len(output) != 1
                    or len(output[0]) != 4
                    or any(not math.isfinite(x) for x in output[0])
                ):
                    raise ValueError("invalid output")
                errors.append(max(abs(x - y) for x, y in zip(output[0], reference[0], strict=True)))
            if (
                report["model_digest"] != signature(report["model_spec"])
                or len(report["maximum_absolute_errors"]) != ROUNDS
                or max(errors) > 0.002
                or any(
                    not math.isclose(x, y, rel_tol=1e-5, abs_tol=1e-8)
                    for x, y in zip(errors, report["maximum_absolute_errors"], strict=True)
                )
            ):
                raise ValueError("numerical evidence mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Intel NPU numerical reference did not pass") from exc
    if runtime == "rockchip-resnet18" and (
        report["expected_class"] != 812
        or len(report["predictions"]) != ROUNDS
        or any(p != 812 for p in report["predictions"])
    ):
        raise ValueError("official RKNN example did not pass")
    quality = QualityPolicy(
        metric=report["quality_metric"],
        minimum=1,
        maximum_peak_memory_mib=1024,
        minimum_repeats=3,
        max_profile_age_seconds=7200,
    )
    identity = WorkloadIdentity(
        code_digest=report["runner_digest"],
        model_digest=report["model_digest"],
        dataset_version="generated-fixed-input:" + report["input_digest"],
        config_digest=signature(
            {
                "runtime": runtime,
                "seed": SEED,
                "rounds": ROUNDS,
                "quality_scope": report["quality_scope"],
            }
        ),
        model_name=runtime,
        task_type="inference",
        input_shape=shape,
        batch_size=1,
        precision=report["precision"],
        seed=SEED,
        work_units=ROUNDS,
        quality_contract_digest=signature(quality),
        measurement_boundary=BOUNDARY,
    )
    environment = signature(
        {
            "qualified_image": image,
            "runtime": report["runtime_versions"],
            "runner": report["runner_digest"],
            "model": report["model_digest"],
        }
    )
    context = ExecutionContext(
        arch=report["arch"],
        environment_digest=environment,
        runtime_versions=report["runtime_versions"],
        accelerator_model=report["accelerator_model"],
        memory_model="host" if vendor == "intel" else "discrete",
        power_mode="not-measured",
        allocation_mode="physical_device",
        resources=Resources(host_cpu=1, host_memory_mib=1024, accelerator_count=1),
    )
    cap = CapabilitySnapshot(
        ref="cap-" + ref,
        node_ref=node,
        backend_cluster_id=cluster,
        backend="kubernetes",
        observed_at=now(),
        valid_for_seconds=7200,
        ready=True,
        arch=context.arch,
        accelerator_vendor=vendor,
        accelerator_model=context.accelerator_model,
        device_class="npu",
        allocation_mode=context.allocation_mode,
        allocatable_count=1,
        host_cpu=1,
        host_memory_mib=1024,
        resource_key=key,
        runtime_versions=context.runtime_versions,
        environment_digest=environment,
        memory_model=context.memory_model,
        power_mode=context.power_mode,
        reservation_verified=True,
        isolation_verified=False,
        evidence_refs=(evidence_ref,),
    )
    variant = RuntimeVariant(
        ref="v-" + ref,
        workload_ref=ref,
        project_ref=project,
        workload_signature=signature(identity),
        arch=context.arch,
        accelerator_vendor=vendor,
        device_class="npu",
        precision=identity.precision,
        model_digest=identity.model_digest,
        compiled_artifact_digest=(
            identity.model_digest if vendor in {"mobilint", "rockchip"} else None
        ),
        image=image,
        environment_digest=environment,
        command=("python3", "/opt/npu_probe.py", "--binding", "/opt/npu-binding.json"),
        verification="MODEL_VERIFIED",
        validation_refs=(evidence_ref,),
        supported_shapes=(shape,),
        runtime_versions=context.runtime_versions,
    )
    spec = WorkloadSpec(
        ref=ref,
        project_ref=project,
        identity=identity,
        candidates=(
            Candidate(
                ref="device",
                variant_ref=variant.ref,
                capability_ref=cap.ref,
                backend="kubernetes",
                context=context,
            ),
        ),
        baseline_candidate_ref="device",
        quality=quality,
        execution=ExecutionPolicy(max_run_seconds=120, max_queue_seconds=300),
    )
    binding = {
        "identity": identity.model_dump(mode="json"),
        "context": context.model_dump(mode="json"),
        "model_digest": identity.model_digest,
        "accelerator_model": context.accelerator_model,
        "runtime": runtime,
        "minimum_quality": 1,
    }
    return {
        "workload": spec.model_dump(mode="json"),
        "variant": variant.model_dump(mode="json"),
        "capability": cap.model_dump(mode="json"),
        "binding": binding,
    }
