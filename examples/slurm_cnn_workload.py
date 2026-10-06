"""Python 3.10 native producer for the platform's v1 result contract.

The control plane retains Python >=3.11 and validates the envelope independently.
Install beside qualify_slurm_torch.py; both files belong in the native manifest.
This fixed generated fixture checks numerical agreement, not model accuracy.
"""

import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import statistics
from pathlib import Path

VERSIONS = {"pytorch": "2.5.0a0+872d972e41.nv24.08", "cuda": "12.6"}


def request_identity(env, machine):
    identity = {}
    for field in ("job_id", "attempt_id", "workload_signature", "context_signature"):
        value = env["RA_" + field.upper()]
        pattern = (
            r"sha256:[a-f0-9]{64}"
            if field.endswith("signature")
            else r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"
        )
        if not re.fullmatch(pattern, value):
            raise ValueError("invalid result identity")
        identity[field] = value
    identity["epoch"] = int(env["RA_EPOCH"])
    if identity["epoch"] < 1:
        raise ValueError("invalid attempt epoch")
    context = json.loads(env["RA_CONTEXT_JSON"])
    if (
        machine != "aarch64"
        or context["arch"] != "arm64"
        or context["accelerator_model"] != "Orin"
        or context["memory_model"] != "unified"
        or context["allocation_mode"] != "physical_device"
        or context["runtime_versions"] != VERSIONS
        or context["parameters"] != {}
        or context["resources"] != {"host_cpu": 1, "host_memory_mib": 1024, "accelerator_count": 1}
        or json.loads(env["RA_INPUT_SHAPE"]) != [4, 3, 32, 32]
        or env["RA_PRECISION"] != "fp32"
        or env["RA_SEED"] != "20261006"
        or env["RA_WORK_UNITS"] != "10"
        or env["RA_EXECUTION_MODE"] not in {"fixed", "observe", "confirmation"}
        or any(env.get(k) for k in ("RA_SAMPLING_PLAN_JSON", "RA_THERMAL_POLICY_JSON"))
    ):
        raise ValueError("request differs from the fixed native CNN contract")
    return identity


def envelope(identity, measured):
    samples = measured["samples_seconds"]
    if (
        measured["result"] != "PASS"
        or measured["compared_elements"] != 400
        or measured["measured_forwards"] != 10
        or len(samples) != 10
        or any(not math.isfinite(x) or x <= 0 for x in samples)
    ):
        raise ValueError("complete measured CNN evidence required")
    elapsed = sum(samples)
    ordered = sorted(samples)
    result = {
        "schema_version": "v1",
        **identity,
        "outcome": "COMPLETED",
        "measured": True,
        "evidence_kind": "hardware",
        "error_code": None,
        "measurements": {
            "elapsed_seconds": elapsed,
            "peak_memory_mib": measured["peak_tensor_allocation_mib"],
            "quality_value": 1.0,
            "sample_count": 10,
            "work_units": 10,
            "latency_p50_ms": statistics.median(samples) * 1000,
            "latency_p95_ms": ordered[int(9 * 0.95)] * 1000,
            "latency_p99_ms": ordered[int(9 * 0.99)] * 1000,
            # A work unit is one four-input forward, not one input image.
            "throughput": 10 / elapsed,
            "gpu_utilization": None,
            "power_watts": None,
            "temperature_celsius": None,
        },
    }
    encoded = json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return {"result": result, "digest": "sha256:" + hashlib.sha256(encoded).hexdigest()}


def main():
    identity = request_identity(os.environ, platform.machine())
    # -I omits the script directory from sys.path. Load only this pinned sibling.
    path = Path(__file__).resolve().with_name("qualify_slurm_torch.py")
    spec = importlib.util.spec_from_file_location("qualified_cnn", path)
    qualifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qualifier)
    measured = qualifier.main(
        [
            "--expected-torch",
            VERSIONS["pytorch"],
            "--expected-cuda",
            VERSIONS["cuda"],
            "--require-native-arch",
            "--enforced-memory-mib",
            "1024",
            "--expected-device",
            "Orin",
        ]
    )
    print("RESOURCE_ADVISOR_RESULT " + json.dumps(envelope(identity, measured), allow_nan=False))


if __name__ == "__main__":
    main()
