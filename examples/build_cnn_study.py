"""Build a consented one-variable study from operator-qualified CNN contracts.

No API calls or hardware qualification are performed. Inputs must be fresh actual
qualification records for cnn_diagnostic, not the historical public report.
"""

import argparse
import json
from pathlib import Path

from resource_advisor.cnn_diagnostic import BOUNDARY
from resource_advisor.contracts import (
    Candidate,
    CapabilitySnapshot,
    ExecutionContext,
    ProfilingPolicy,
    QualityPolicy,
    Resources,
    RuntimeVariant,
    WorkloadIdentity,
    WorkloadSpec,
    signature,
)
from resource_advisor.policy import compatibility


def build(cap, variant, identity, quality):
    command = ("python", "-m", "resource_advisor.cnn_diagnostic")
    if (
        variant.command != command
        or variant.pilot_command != command
        or identity.measurement_boundary != BOUNDARY
        or identity.input_shape != (1, 3, 128, 128)
        or identity.precision != "fp32"
        or identity.task_type != "inference"
        or not 3 <= identity.work_units <= 30
        or identity.model_digest != variant.model_digest
        or signature(identity) != variant.workload_signature
        or cap.device_class != "gpu"
        or cap.accelerator_vendor != "nvidia"
        or cap.allocation_mode != "physical_device"
    ):
        raise ValueError("qualified CNN runtime and matching identity required")
    context = ExecutionContext(
        arch=cap.arch,
        environment_digest=cap.environment_digest,
        runtime_versions=cap.runtime_versions,
        accelerator_model=cap.accelerator_model,
        memory_model=cap.memory_model,
        power_mode=cap.power_mode,
        allocation_mode=cap.allocation_mode,
        resources=Resources(host_cpu=1, host_memory_mib=2048, accelerator_count=1),
    )
    unit = f"{cap.backend}:gpu:{cap.accelerator_model}:{cap.allocation_mode}"
    spec = WorkloadSpec(
        ref=variant.workload_ref,
        project_ref=variant.project_ref,
        identity=identity,
        candidates=tuple(
            Candidate(
                ref=strategy,
                variant_ref=variant.ref,
                capability_ref=cap.ref,
                backend=cap.backend,
                context=context.model_copy(update={"parameters": {"input_strategy": strategy}}),
            )
            for strategy in ("recompute", "cache")
        ),
        baseline_candidate_ref="recompute",
        quality=quality,
        profiling=ProfilingPolicy(
            consent=True,
            max_candidates=2,
            max_probes=2,
            mutable_parameters=("input_strategy",),
            max_wall_seconds_per_candidate=60,
            total_wall_seconds=600,
            final_validation_seconds=240,
            device_seconds={unit: 600},
        ),
    )
    for candidate in spec.candidates:
        errors = compatibility(spec, candidate, variant, cap)
        if errors:
            raise ValueError("qualification unavailable: " + ",".join(errors))
    return spec


def main():
    parser = argparse.ArgumentParser()
    for name in ("capability", "variant", "identity", "quality", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    spec = build(
        *[
            cls.model_validate_json(getattr(args, name).read_text())
            for name, cls in (
                ("capability", CapabilitySnapshot),
                ("variant", RuntimeVariant),
                ("identity", WorkloadIdentity),
                ("quality", QualityPolicy),
            )
        ]
    )
    with args.output.open("x") as output:
        output.write(json.dumps(spec.model_dump(mode="json"), indent=2) + "\n")


if __name__ == "__main__":
    main()
