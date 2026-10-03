"""Contract-bound ResNet-v1-50 Hailo runner; no host inference fallback.

The immutable image contains both the binding and the qualified fixtures.
Memory measures host RSS, not accelerator memory. Full per-image evidence is
emitted separately from the platform result for retention in the qualification
registry. Failed agreement/loss gates never produce performance measurements.
"""

import argparse
import json
import os
from pathlib import Path

from .contracts import (
    Contract,
    Digest,
    ExecutionContext,
    ExecutionResult,
    Measurements,
    WorkloadIdentity,
    signature,
)
from .hailo_qualification import qualify, quality_gates

MODEL = "sha256:2625772f5e1324c1e2781856926a445b4fe6494b9e0bb0161a7d86df7f095d38"
HEF = "sha256:a1d82e9121c66e772257490cb3af904d1e90fb4387ad5f683fbc5efe1a05f9f7"
MANIFEST = "sha256:7740c3420918e1264024dd41dbe6c0db18a2f1d283789afc89ef3d0db522d30f"
BOUNDARY = "synchronous-host-float32-NHWC-to-NPU-output; memory=host-process-peak-RSS"
PRECISION = "hailo-compiled-float32-io"


class Binding(Contract):
    identity: WorkloadIdentity
    context: ExecutionContext
    hef_digest: Digest
    manifest_digest: Digest


def validate_binding(binding, env):
    identity, context = binding.identity, binding.context
    if (
        identity.model_digest != MODEL
        or binding.hef_digest != HEF
        or binding.manifest_digest != MANIFEST
        or identity.input_shape != (1, 224, 224, 3)
        or identity.batch_size != 1
        or identity.work_units != 100
        or identity.precision != PRECISION
        or identity.seed != 20261005
        or identity.task_type != "inference"
        or identity.measurement_boundary != BOUNDARY
    ):
        raise ValueError("unqualified Hailo workload binding")
    if (
        context.arch != "arm64"
        or context.accelerator_model != "HAILO8"
        or context.allocation_mode != "physical_device"
        or context.resources.accelerator_count != 1
        or context.resources.host_cpu != 1
        or context.resources.host_memory_mib != 1024
        or context.parameters
        or context.runtime_versions.get("hailort") != "4.23.0"
        or context.runtime_versions.get("driver") != "4.23.0"
    ):
        raise ValueError("unqualified Hailo execution context")
    if (
        ExecutionContext.model_validate_json(env["RA_CONTEXT_JSON"]) != context
        or env["RA_WORKLOAD_SIGNATURE"] != signature(identity)
        or json.loads(env["RA_INPUT_SHAPE"]) != list(identity.input_shape)
        or int(env["RA_WORK_UNITS"]) != identity.work_units
        or env["RA_PRECISION"] != identity.precision
        or int(env["RA_SEED"]) != identity.seed
        or env["RA_EXECUTION_MODE"] not in {"observe", "fixed"}
    ):
        raise ValueError("submitted contract differs from immutable Hailo binding")
    # Validate result identity before importing the device runtime or acquiring it.
    return ExecutionResult(
        job_id=env["RA_JOB_ID"],
        attempt_id=env["RA_ATTEMPT_ID"],
        epoch=int(env["RA_EPOCH"]),
        workload_signature=env["RA_WORKLOAD_SIGNATURE"],
        context_signature=env["RA_CONTEXT_SIGNATURE"],
        outcome="FAILED",
        evidence_kind="hardware",
        error_code="HAILO_QUALITY_GATE_FAILED",
    )


def run(binding, fixture, hef, *, env=None, execute=qualify):
    env = os.environ if env is None else env
    result = validate_binding(binding, env)
    report = execute(fixture, hef, binding.hef_digest[7:], binding.manifest_digest[7:])
    if (
        report["original_model_sha256"] != binding.identity.model_digest[7:]
        or report["hef_sha256"] != binding.hef_digest[7:]
        or report["manifest_sha256"] != binding.manifest_digest[7:]
        or report["architecture"] != binding.context.accelerator_model
        or report["device_count"] != 1
        or report["measured_images"] != 100
        or len(report["predictions"]) != 100
        or report["warmup_images"] != 4
        or report["measurement_boundary"] != BOUNDARY.split(";")[0]
        or any(report.get(k) != v for k, v in binding.context.runtime_versions.items())
    ):
        raise ValueError("actual Hailo report differs from qualified binding")
    rows = report["predictions"]
    quality = quality_gates(
        [r["prediction"] for r in rows],
        [r["label"] for r in rows],
        [r["reference_top1"] for r in rows],
    )
    if quality != report["quality"]:
        raise ValueError("reported Hailo quality does not match predictions")
    if quality["qualified"]:
        measurements = Measurements(
            elapsed_seconds=report["elapsed_seconds"],
            peak_memory_mib=report["host_process_peak_rss_mib"],
            quality_value=quality["accuracy"],
            sample_count=100,
            work_units=100,
            latency_p50_ms=report["latency_p50_ms"],
            latency_p95_ms=report["latency_p95_ms"],
            latency_p99_ms=report["latency_p99_ms"],
            throughput=report["throughput_images_per_second"],
        )
        result = ExecutionResult.model_validate(
            {
                **result.model_dump(mode="json"),
                "outcome": "COMPLETED",
                "error_code": None,
                "measurements": measurements.model_dump(mode="json"),
            }
        )
    print("RESOURCE_ADVISOR_HAILO_REPORT " + json.dumps(report, allow_nan=False), flush=True)
    print(
        "RESOURCE_ADVISOR_RESULT "
        + json.dumps(
            {"result": result.model_dump(mode="json"), "digest": signature(result)},
            allow_nan=False,
        ),
        flush=True,
    )
    # A valid FAILED result must reach the collector. Process success indicates
    # delivery of an envelope, not passage of the model gates.
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--hef", required=True, type=Path)
    args = parser.parse_args()
    run(Binding.model_validate_json(args.binding.read_text()), args.fixture, args.hef)


if __name__ == "__main__":
    main()
