"""Render one CUDA target from an actual successful qualification report.

The report must come from cuda_probe --qualify under a Kubernetes accelerator
reservation. This only renders contracts; the operator supplies node, image and
queue routing separately. It does not certify AI models or device isolation.
"""

import argparse
import hashlib
import json
from pathlib import Path

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
from resource_advisor.cuda_probe import BOUNDARY, PTX, ROUNDS, SIZE


def build(report, *, ref, project, node, image, shared=False):
    duration = report.get("sustained_seconds", 0)
    boundary = BOUNDARY if not duration else BOUNDARY + f"-timed-{duration}s-windows"
    if (
        duration not in {0, 30, 60, 90}
        or report["quality_value"] != 1
        or report["elements_checked"] != SIZE
        or report["kernel_sha256"] != hashlib.sha256(PTX).hexdigest()
        or report["measurement_boundary"] != boundary
        or len(report["samples_seconds"]) != ROUNDS
        or any(not 0 < t < 60 for t in report["samples_seconds"])
        or (duration and sum(report["samples_seconds"]) < duration)
        or (duration and report.get("kernel_launches", 0) <= ROUNDS + 2)
    ):
        raise ValueError("A successful matching CUDA qualification is required")
    quality = QualityPolicy(
        metric="integer_element_agreement", minimum=1, maximum_peak_memory_mib=1
    )
    kernel = "sha256:" + report["kernel_sha256"]
    code = (
        "sha256:"
        + hashlib.sha256(
            (Path(__file__).parents[1] / "src/resource_advisor/cuda_probe.py").read_bytes()
        ).hexdigest()
    )
    identity = WorkloadIdentity(
        code_digest=code,
        model_digest=kernel,
        dataset_version="generated-index-0-to-4095-v1",
        config_digest=signature(
            {
                "size": SIZE,
                "rounds": ROUNDS,
                "warmups": 2,
                **({"sustained_seconds": duration} if duration else {}),
            }
        ),
        task_type="benchmark",
        input_shape=(SIZE,),
        batch_size=1,
        precision="int32",
        work_units=ROUNDS,
        quality_contract_digest=signature(quality),
        measurement_boundary=boundary,
    )
    environment = signature({"image": image, "runtime": report["runtime_versions"], "code": code})
    mode = "virtual_slot" if shared else "physical_device"
    context = ExecutionContext(
        arch=report["arch"],
        environment_digest=environment,
        runtime_versions=report["runtime_versions"],
        accelerator_model=report["accelerator_model"],
        memory_model="unified" if report["arch"] == "arm64" else "discrete",
        power_mode="observed-default",
        allocation_mode=mode,
        resources=Resources(host_cpu=1, host_memory_mib=512, accelerator_count=1),
        parameters={"sustained_seconds": duration} if duration else {},
    )
    cap = CapabilitySnapshot(
        ref="cap-" + ref,
        node_ref=node,
        backend_cluster_id="lab-" + ref,
        backend="kubernetes",
        observed_at=now(),
        valid_for_seconds=3600,
        ready=True,
        arch=context.arch,
        accelerator_vendor="nvidia",
        accelerator_model=context.accelerator_model,
        device_class="gpu",
        allocation_mode=mode,
        allocatable_count=1,
        host_cpu=1,
        host_memory_mib=512,
        resource_key="nvidia.com/gpu.shared" if shared else "nvidia.com/gpu",
        runtime_versions=context.runtime_versions,
        environment_digest=environment,
        memory_model=context.memory_model,
        power_mode=context.power_mode,
        reservation_verified=True,
        isolation_verified=False,
        evidence_refs=("qual-" + ref,),
    )
    variant = RuntimeVariant(
        ref="v-" + ref,
        workload_ref=ref,
        project_ref=project,
        workload_signature=signature(identity),
        arch=context.arch,
        accelerator_vendor="nvidia",
        device_class="gpu",
        precision="int32",
        model_digest=kernel,
        image=image,
        environment_digest=environment,
        command=(
            "python",
            "/opt/cuda_probe.py",
            *(["--sustain-seconds", str(duration)] if duration else []),
        ),
        verification="MODEL_VERIFIED",
        validation_refs=("qual-" + ref,),
        supported_shapes=((SIZE,),),
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
        execution=ExecutionPolicy(
            max_run_seconds=max(60, duration + 60), max_queue_seconds=300 if duration else 180
        ),
    )
    return {
        k: v.model_dump(mode="json")
        for k, v in [("capability", cap), ("variant", variant), ("workload", spec)]
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["qualification", "ref", "project", "node", "image", "output"]:
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--shared", action="store_true")
    args = parser.parse_args()
    result = build(
        json.loads(Path(args.qualification).read_text()),
        ref=args.ref,
        project=args.project,
        node=args.node,
        image=args.image,
        shared=args.shared,
    )
    with Path(args.output).open("x") as f:
        f.write(json.dumps(result, indent=2) + "\n")
