"""Finite-input CUDA Gram-matrix benchmark; numerical correctness, not model accuracy.

Input staging/reference calculation are outside the forward-only interval and
inside Job wall cost. No CPU execution fallback or thermal qualification claim.
"""

import argparse
import hashlib
import json
import os
import platform
import stat
import statistics
import tempfile
import time
from pathlib import Path

from .contracts import ExecutionResult, Measurements, ThermalPolicy, signature
from .sampling import SamplingPlan, SamplingSession, directory_reader
from .thermal import NvmlReader, ThermalTrace, Window

BOUNDARY = "per-input-cuda-gram-forward-and-synchronize;staging-reference-copy-excluded-v1"


def stage_inputs(plan, source, destination):
    """Copy bounded operator-owned projected files into a fresh private directory.

    Following projected-volume symlinks is intentional here; actual copied bytes
    must match the approved plan, then SamplingSession rechecks each regular file.
    The operator owns the source root. The workload cannot replace its originals.
    """
    plan = SamplingPlan.model_validate(plan)
    destination.mkdir(mode=0o700, exist_ok=False)
    for sample in plan.samples:
        fd = os.open(source / (sample.ref + ".json"), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("projected input must resolve to a regular file")
            raw = handle.read(1048577)
        if (
            len(raw) > 1048576
            or "sha256:" + hashlib.sha256(raw).hexdigest() != sample.content_digest
        ):
            raise ValueError("projected input differs from approved bounded content")
        path = destination / (sample.ref + ".json")
        with path.open("xb") as handle:
            handle.write(raw)
        path.chmod(0o400)
    return directory_reader(destination)


def validate_context(torch, context, plan, environment, driver_version=None):
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable; refusing CPU fallback")
    if context["resources"]["accelerator_count"] != 1 or torch.cuda.device_count() != 1:
        raise RuntimeError("exactly one visible allocated GPU required")
    if context["allocation_mode"] != "physical_device":
        raise RuntimeError("sampled shared GPU qualification is not implemented")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if arch != context["arch"] or torch.cuda.get_device_name(0) != context["accelerator_model"]:
        raise RuntimeError("actual hardware differs from approved execution context")
    actual = {"pytorch": torch.__version__, "cuda": torch.version.cuda}
    if driver_version is not None:
        actual["driver"] = driver_version
    if any(actual.get(k) != v for k, v in context["runtime_versions"].items()):
        raise RuntimeError("runtime version differs from approved execution context")
    shape = plan.input_shape
    if len(shape) != 2 or shape[0] != shape[1] or not 16 <= shape[0] <= 256:
        raise ValueError("sampled Gram benchmark requires square matrices of size 16–256")
    if (
        tuple(json.loads(environment["RA_INPUT_SHAPE"])) != shape
        or int(environment["RA_WORK_UNITS"]) != plan.work_units
        or int(environment["RA_SEED"]) != plan.seed
        or environment["RA_PRECISION"] != plan.precision
        or plan.batch_size != 1
    ):
        raise ValueError("sampling plan differs from execution identity")


def run(source):
    import torch

    raw = os.environ.get("RA_THERMAL_POLICY_JSON")
    if raw is not None:
        policy = ThermalPolicy.model_validate_json(raw)
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable; refusing CPU fallback")
        with NvmlReader(str(torch.cuda.get_device_properties(0).uuid)) as reader:
            return _run(source, torch, reader, policy)
    return _run(source, torch, None, None)


def _run(source, torch, thermal_reader, thermal_policy):

    plan = SamplingPlan.model_validate_json(os.environ["RA_SAMPLING_PLAN_JSON"])
    context = json.loads(os.environ["RA_CONTEXT_JSON"])
    validate_context(
        torch, context, plan, os.environ, thermal_reader.driver_version if thermal_reader else None
    )
    if thermal_reader and (
        thermal_reader.driver_version != thermal_policy.driver_version
        or thermal_reader.device_uuid_digest != thermal_policy.device_uuid_digest
        or plan.work_units > 32
    ):
        raise RuntimeError(
            "thermal qualification differs from device/driver or bounded work budget"
        )
    torch.set_num_threads(max(1, int(context["resources"]["host_cpu"])))
    torch.manual_seed(plan.seed)
    torch.backends.cuda.matmul.allow_tf32 = False
    dtype = torch.float32 if plan.precision == "fp32" else torch.float16
    tolerance = 1e-4 if plan.precision == "fp32" else 0.01
    measuring, windows = False, []

    def operation(batch):
        cpu = torch.tensor(batch.values, dtype=dtype).reshape(batch.shape)
        reference = cpu.double() @ cpu.double().T
        tensor = cpu.cuda()
        torch.cuda.synchronize()
        before = thermal_reader.read() if thermal_reader and measuring else None
        started = time.perf_counter()
        output = tensor @ tensor.T
        torch.cuda.synchronize()
        finished = time.perf_counter()
        elapsed = finished - started
        if before is not None:
            after = thermal_reader.read()
            windows.append(
                Window(
                    sample_ref=plan.samples[len(windows)].ref,
                    before=before,
                    forward_started=started - thermal_reader.origin,
                    forward_finished=finished - thermal_reader.origin,
                    after=after,
                )
            )
        quality = torch.isclose(output.cpu().double(), reference, rtol=tolerance, atol=tolerance)
        return {"elapsed_seconds": elapsed, "quality": quality.double().mean().item()}

    session = SamplingSession(plan)
    with tempfile.TemporaryDirectory(prefix="ra-sampled-") as root, torch.inference_mode():
        reader = stage_inputs(plan, source, Path(root) / "inputs")
        session.warmup(reader, operation)
        torch.cuda.reset_peak_memory_stats()
        measuring = True
        samples = [session.measure_next(reader, operation) for _ in range(plan.work_units)]
        peak_memory = torch.cuda.max_memory_allocated() / 1024**2
    latencies = [s["elapsed_seconds"] for s in samples]
    ordered = sorted(latencies)
    elapsed = sum(latencies)
    result = ExecutionResult(
        job_id=os.environ["RA_JOB_ID"],
        attempt_id=os.environ["RA_ATTEMPT_ID"],
        epoch=int(os.environ["RA_EPOCH"]),
        workload_signature=os.environ["RA_WORKLOAD_SIGNATURE"],
        context_signature=os.environ["RA_CONTEXT_SIGNATURE"],
        outcome="COMPLETED",
        evidence_kind="hardware",
        measurements=Measurements(
            elapsed_seconds=elapsed,
            peak_memory_mib=peak_memory,
            quality_value=min(s["quality"] for s in samples),
            sample_count=plan.work_units,
            work_units=plan.work_units,
            latency_p50_ms=statistics.median(latencies) * 1000,
            latency_p95_ms=ordered[int((len(ordered) - 1) * 0.95)] * 1000,
            latency_p99_ms=ordered[int((len(ordered) - 1) * 0.99)] * 1000,
            throughput=plan.work_units / elapsed,
        ),
    )
    receipt = session.receipt(result)
    print(
        "RA_SAMPLED_METRICS "
        + json.dumps(
            {
                "boundary": BOUNDARY,
                "selection_digest": plan.selection_digest,
                "samples": [
                    {"ref": s.ref, **m} for s, m in zip(plan.samples, samples, strict=True)
                ],
            },
            allow_nan=False,
        ),
        flush=True,
    )
    envelope = {
        "result": result.model_dump(mode="json"),
        "digest": signature(result),
        "sampling_receipt": receipt.model_dump(mode="json"),
    }
    if thermal_reader:
        envelope["thermal_trace"] = ThermalTrace(
            job_id=result.job_id,
            attempt_id=result.attempt_id,
            result_digest=signature(result),
            policy_digest=signature(thermal_policy),
            driver_version=thermal_reader.driver_version,
            device_uuid_digest=thermal_reader.device_uuid_digest,
            windows=tuple(windows),
        ).model_dump(mode="json")
    encoded = json.dumps(envelope, allow_nan=False)
    if len(encoded) > 65536:
        raise ValueError("result exceeds bounded collector envelope")
    print("RESOURCE_ADVISOR_RESULT " + encoded, flush=True)
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    run(parser.parse_args().inputs)


if __name__ == "__main__":
    main()
