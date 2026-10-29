"""Bounded accelerator batch that stages an exact input object, then infers over it.

The staging half is ``staged_workload``'s: ``network_profile.stage`` copies (cold)
or verifies (hot) the declared object into the destination or plan-bound cache,
with the same receipt, Downward API identity and result envelope. The compute half
feeds exactly those staged bytes to a qualified native runtime instead of hashing
them on the CPU:

* ``cuda``/``hailo``: ``inference_runtime.Runtime`` (same-source ResNet50, startup
  qualification on the fixed 100-image fixture). The staged object is an
  ``(N, 224, 224, 3)`` float32 ``.npy`` whose every tensor is a qualified fixture
  sample; quality is top-1 agreement with the manifest's ``reference_top1``.
* ``mobilint-candy``: ``npu_probe.mobilint`` (qualified Candy style model) on an
  ``(N, 224, 344, 3)`` float32 ``.npy``; quality is output repeatability only, a
  different source model, never comparable with ResNet50 results.

The accepted workload identity is passed as JSON (``--workload-identity``, or
``RA_WORKLOAD_IDENTITY_JSON``) and must hash to the job's ``RA_WORKLOAD_SIGNATURE``;
its ``model_digest`` names the source model the native runtime must report, its
``dataset_version`` must be ``sha256:`` + the staged object digest, its
``input_shape`` the staged array shape and its ``work_units`` ``RA_WORK_UNITS``.

Order: identity binding → stage → re-read and verify the staged bytes → decode/shape
checks → native initialization and qualification (residual preparation) → runtime
model identity check → timed passes. ``elapsed_seconds`` covers only the passes (``RA_WORK_UNITS``
of them, each over every staged tensor). Any failure is reported FAILED without a
staging receipt. Native SDK libraries must be present; there is no CPU fallback.

``python -m resource_advisor.staged_accelerator_workload --runtime cuda
--workload-identity JSON --source URL --sha256 HEX --size-bytes N
--source-endpoint-ref REF --cache-dir DIR [--config model.json] [--fixture DIR]``
"""

import argparse
import hashlib
import io
import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path

from .contracts import ExecutionResult, Measurements, WorkloadIdentity, now, signature
from .network_profile import (
    StagingReceipt,
    Unavailable,
    execution_identity,
    stage,
    storage_identity,
)
from .staged_workload import _emit

MAX_INPUT_BYTES = 32 * 1024**2
RESNET_CONTRACT = "rgb-f32-nhwc-224x224x3-v1"
MOBILINT_CONTRACT = "f32-hwc-224x344x3-mobilint-candy-v1"
SHAPES = {"cuda": (224, 224, 3), "hailo": (224, 224, 3), "mobilint-candy": (224, 344, 3)}
# What the executable actually does: per-tensor (batch 1) inference, timed per pass
# from staged host input to native output, at each runtime's qualified precision.
# A declaration of any other semantics is a different workload, even when signed.
BOUNDARY = "staged-host-input-to-native-output-per-pass"
PRECISION = {
    "cuda": "fp32",
    "hailo": "hailo-compiled-float32-io",
    "mobilint-candy": "mxq-float32-io",
}


class Failed(Exception):
    pass


def _arguments(argv, env):
    p = argparse.ArgumentParser(prog="python -m resource_advisor.staged_accelerator_workload")
    p.add_argument("--runtime", choices=sorted(SHAPES), required=True)
    p.add_argument("--workload-identity", default=env.get("RA_WORKLOAD_IDENTITY_JSON"))
    p.add_argument("--config", type=Path, default=Path("/opt/inference/model.json"))
    p.add_argument("--fixture", type=Path, default=Path("/opt/fixture"))
    p.add_argument("--source", default=env.get("RA_DATASET_SOURCE"))
    p.add_argument("--sha256", default=env.get("RA_DATASET_SHA256"))
    p.add_argument("--size-bytes", type=int, default=env.get("RA_DATASET_SIZE_BYTES"))
    p.add_argument("--source-endpoint-ref", default=env.get("RA_DATASET_SOURCE_ENDPOINT"))
    p.add_argument("--cache-dir", default=env.get("RA_DATASET_CACHE_DIR"))
    args = p.parse_args(argv)
    if None in (
        args.source,
        args.sha256,
        args.size_bytes,
        args.source_endpoint_ref,
        args.workload_identity,
    ):
        p.error("workload identity, dataset source, sha256, size and source endpoint are required")
    return args


def _tensors(path, digest, runtime, declared_shape):
    """The staged object's own bytes, re-verified, as validated float32 tensors."""
    import numpy as np

    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise Failed("STAGED_INPUT_INVALID") from None
    if hashlib.sha256(raw).hexdigest() != digest:
        raise Failed("STAGED_INPUT_DIGEST_MISMATCH")
    try:
        data = np.load(io.BytesIO(raw), allow_pickle=False)
    except (ValueError, OSError, EOFError):
        raise Failed("STAGED_INPUT_INVALID") from None
    if (
        not isinstance(data, np.ndarray)
        or data.dtype != np.float32
        or data.ndim != 4
        or data.shape[0] < 1
        or tuple(data.shape[1:]) != SHAPES[runtime]
        or tuple(data.shape) != declared_shape
        or not np.isfinite(data).all()
        or (runtime != "mobilint-candy" and (data.min() < 0 or data.max() > 255))
    ):
        raise Failed("STAGED_INPUT_CONTRACT_MISMATCH")
    return data


def _resnet(args, model, data, iterations, factory):
    import numpy as np

    try:
        config = json.loads(args.config.read_text())
    except (OSError, ValueError):
        raise Failed("MODEL_CONFIG_INVALID") from None
    if not isinstance(config, dict) or config.get("runtime") != args.runtime:
        raise Failed("MODEL_IDENTITY_MISMATCH")
    started = time.perf_counter()
    try:
        runtime = factory(config, args.fixture)
    except Exception:  # noqa: BLE001 -- vendor SDK/qualification failures differ
        raise Failed("NATIVE_INITIALIZATION_FAILED") from None
    preparation = time.perf_counter() - started
    try:
        identity = runtime.identity
        if (
            "sha256:" + identity["source_model_sha256"] != model
            or identity["input_contract"] != RESNET_CONTRACT
        ):
            raise Failed("MODEL_IDENTITY_MISMATCH")
        try:
            manifest_raw = (args.fixture / "manifest.json").read_bytes()
        except OSError:
            raise Failed("QUALIFICATION_MANIFEST_INVALID") from None
        if "sha256:" + hashlib.sha256(manifest_raw).hexdigest() != identity["qualification_digest"]:
            raise Failed("QUALIFICATION_MANIFEST_MISMATCH")
        try:
            reference = {
                s["tensor_sha256"]: s["reference_top1"] for s in json.loads(manifest_raw)["samples"]
            }
        except (ValueError, KeyError, TypeError):
            raise Failed("QUALIFICATION_MANIFEST_INVALID") from None
        expected = [reference.get(hashlib.sha256(t.tobytes()).hexdigest()) for t in data]
        if None in expected:
            raise Failed("STAGED_INPUT_NO_QUALIFIED_REFERENCE")
        samples, agree = [], 0
        try:
            for _ in range(iterations):
                begin = time.perf_counter()
                predictions = [int(np.argmax(runtime._scores(t))) for t in data]
                samples.append(time.perf_counter() - begin)
                agree += sum(p == e for p, e in zip(predictions, expected, strict=True))
        except Exception:  # noqa: BLE001 -- any native error invalidates the run
            raise Failed("NATIVE_INFERENCE_FAILED") from None
        report = {
            "identity": identity,
            "runtime_metadata": runtime.metadata,
            "quality_metric": "top1_agreement_with_qualified_reference_top1",
            "quality_scope": "staged-subset-of-qualified-fixture",
        }
        return samples, agree / (iterations * len(data)), preparation, report
    finally:
        stack = getattr(runtime, "stack", None)
        if stack is not None:
            stack.close()


def _mobilint(model, data, iterations, runner):
    from .npu_probe import MOBILINT_MODEL

    if model != "sha256:" + MOBILINT_MODEL:
        raise Failed("MODEL_IDENTITY_MISMATCH")
    started = time.perf_counter()
    try:
        native = runner(inputs=list(data), rounds=iterations)
    except Exception:  # noqa: BLE001 -- vendor SDK exceptions differ
        raise Failed("NATIVE_INFERENCE_FAILED") from None
    samples = native["samples_seconds"]
    if native["model_digest"] != "sha256:" + MOBILINT_MODEL or len(samples) != iterations:
        raise Failed("MODEL_IDENTITY_MISMATCH")
    report = {
        "identity": {
            "model_digest": native["model_digest"],
            "input_contract": MOBILINT_CONTRACT,
            "accelerator_model": native["accelerator_model"],
        },
        "runtime_metadata": native["runtime_versions"],
        "native_input_digest": native["input_digest"],
        "quality_metric": native["quality_metric"],
        "quality_scope": native["quality_scope"],
        "task_accuracy": None,
    }
    # The runner launches the model and repeats warm reference outputs before its
    # timed rounds; that span is preparation, not compute.
    return samples, native["quality_value"], time.perf_counter() - started - sum(samples), report


def run(env=None, *, argv=None, write=print, resnet_runtime=None, mobilint_runner=None):
    env = os.environ if env is None else env
    args = _arguments([] if argv is None else argv, env)
    digest, size = args.sha256, int(args.size_bytes)
    iterations = int(env["RA_WORK_UNITS"])
    if not 1 <= iterations <= 20:
        raise ValueError("staged accelerator workload requires 1–20 work units")
    identity = {
        "job_id": env["RA_JOB_ID"],
        "attempt_id": env["RA_ATTEMPT_ID"],
        "epoch": int(env["RA_EPOCH"]),
        "workload_signature": env["RA_WORKLOAD_SIGNATURE"],
        "context_signature": env["RA_CONTEXT_SIGNATURE"],
    }

    def failed(code):
        result = ExecutionResult(
            **identity, outcome="FAILED", evidence_kind="hardware", error_code=code
        )
        _emit(write, result)
        return result, None

    try:
        declared = WorkloadIdentity.model_validate_json(args.workload_identity)
    except ValueError:
        return failed("WORKLOAD_CONTRACT_MISMATCH")
    if (
        signature(declared) != identity["workload_signature"]
        or declared.dataset_version != "sha256:" + digest
        or declared.work_units != iterations
        or declared.task_type != "inference"
        or (declared.batch_size, declared.global_batch_size, declared.gradient_accumulation)
        != (1, None, 1)
        or declared.precision != PRECISION[args.runtime]
        or declared.measurement_boundary != BOUNDARY
    ):
        return failed("WORKLOAD_CONTRACT_MISMATCH")
    who = execution_identity(env)
    cache_dir, binding_ref = args.cache_dir, None
    if env.get("RA_STAGING_BINDINGS_JSON"):
        binding = json.loads(env["RA_STAGING_BINDINGS_JSON"]).get(who["node_name"])
        if binding is None:
            return failed("STAGING_BINDING_MISSING_FOR_NODE")
        cache_dir, binding_ref = binding["cache_path"], binding["ref"]
    if not cache_dir:
        raise ValueError("a cache directory or plan cache binding is required")
    try:
        path, sample, state, kind, locator = stage(
            args.source, digest, size, cache_dir, max_bytes=MAX_INPUT_BYTES
        )
        staged_at = now()
    except Unavailable as exc:
        return failed(f"STAGING_{exc}")
    try:
        data = _tensors(path, digest, args.runtime, declared.input_shape)
        if args.runtime == "mobilint-candy":
            if mobilint_runner is None:
                from .npu_probe import mobilint as mobilint_runner
            samples, quality, preparation, report = _mobilint(
                declared.model_digest, data, iterations, mobilint_runner
            )
        else:
            if resnet_runtime is None:
                from .inference_runtime import Runtime as resnet_runtime
            samples, quality, preparation, report = _resnet(
                args, declared.model_digest, data, iterations, resnet_runtime
            )
    except Failed as exc:
        return failed(str(exc))
    elapsed = sum(samples)
    report.update(
        runtime=args.runtime,
        input_digest="sha256:" + digest,
        tensor_count=len(data),
        preparation_seconds=preparation,
        samples_seconds=samples,
        measurement_boundary=BOUNDARY,
    )
    write("RESOURCE_ADVISOR_STAGED_ACCELERATOR_REPORT " + json.dumps(report, allow_nan=False))
    result = ExecutionResult(
        **identity,
        outcome="COMPLETED",
        evidence_kind="hardware",
        measurements=Measurements(
            elapsed_seconds=elapsed,
            peak_memory_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            quality_value=quality,
            sample_count=iterations,
            work_units=iterations,
            latency_p50_ms=statistics.median(samples) * 1000,
            throughput=iterations / elapsed,
        ),
    )
    receipt = StagingReceipt(
        job_id=identity["job_id"],
        attempt_id=identity["attempt_id"],
        content_digest="sha256:" + digest,
        size_bytes=size,
        source_endpoint_ref=args.source_endpoint_ref,
        source_kind=kind,
        source_locator=locator,
        observer_host=who["observer_host"],
        identity_source=who["identity_source"],
        node_name=who["node_name"],
        pod_name=who.get("pod_name"),
        pod_uid=who.get("pod_uid"),
        cache_state=state,
        cache_locator=str(path.parent),
        cache_storage_identity=storage_identity(path.parent),
        cache_binding_ref=binding_ref,
        staged_at=staged_at,
        sample=sample,
    )
    _emit(write, result, receipt)
    return result, receipt


if __name__ == "__main__":
    run(argv=sys.argv[1:])
