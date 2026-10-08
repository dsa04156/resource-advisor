"""Frozen Digits MLP on direct Intel NPU; CPU/AUTO/HETERO fallback is forbidden.

FP32 model and I/O are preserved. Intel NPU hardware computes internally in FP16;
the original CUDA fixture's numerical and top-1 gates remain unchanged.
"""

import argparse
import base64
import hashlib
import json
import platform
import re
import time
from pathlib import Path

import numpy as np


def decode_tensor(record, shape):
    raw = base64.b64decode(record["data"], validate=True)
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ValueError("fixture tensor digest mismatch")
    if len(raw) != int(np.prod(shape)) * 4:
        raise ValueError("fixture tensor shape mismatch")
    value = np.frombuffer(raw, dtype="<f4").reshape(shape).copy()
    if not np.isfinite(value).all():
        raise ValueError("nonfinite fixture tensor")
    return value


def load_fixture(path):
    raw = path.read_bytes()
    fixture = json.loads(raw)
    dimensions = [(layer["inputs"], layer["outputs"]) for layer in fixture["layers"]]
    if (
        fixture.get("model") != "trained Digits MLP 64-256-256-10"
        or fixture.get("precision") != "fp32"
        or fixture.get("batch_size") != 256
        or dimensions != [(64, 256), (256, 256), (256, 10)]
        or fixture.get("quality") != {"top1_reference_agreement": 1.0, "atol": 0.003, "rtol": 0.001}
    ):
        raise ValueError("expected frozen Digits MLP and unchanged quality gates")
    inputs = decode_tensor(fixture["input"], (256, 64))
    reference = decode_tensor(fixture["reference"], (256, 10))
    layers = [
        (
            decode_tensor(layer["weights"], (layer["inputs"], layer["outputs"])),
            decode_tensor(layer["bias"], (layer["outputs"],)),
        )
        for layer in fixture["layers"]
    ]
    if len(fixture["labels"]) != 256 or any(
        not isinstance(label, int) or not 0 <= label < 10 for label in fixture["labels"]
    ):
        raise ValueError("invalid frozen labels")
    return fixture, inputs, reference, layers, hashlib.sha256(raw).hexdigest()


def measure_quality(output, reference, labels):
    if output.shape != reference.shape or len(labels) != output.shape[0]:
        raise ValueError("output/reference shape mismatch")
    finite = bool(np.isfinite(output).all() and np.isfinite(reference).all())
    errors = np.abs(output.astype(np.float64) - reference.astype(np.float64))
    close = finite and bool(np.all(errors <= 0.003 + 0.001 * np.abs(reference)))
    predicted = np.argmax(output, axis=1)
    expected = np.argmax(reference, axis=1)
    agreement = float(np.mean(predicted == expected))
    return dict(
        quality=agreement,
        quality_passed=close and agreement == 1.0,
        accuracy=float(np.mean(predicted == np.asarray(labels))),
        max_absolute_error=float(errors.max()) if finite else None,
        numerical_close=close,
        absolute_tolerance=0.003,
        relative_tolerance=0.001,
    )


def compile_on_npu(core, model):
    if not any(d == "NPU" or d.startswith("NPU.") for d in core.available_devices):
        raise ValueError("direct NPU unavailable; CPU fallback forbidden")
    compiled = core.compile_model(model, "NPU", {"PERFORMANCE_HINT": "LATENCY"})
    # Preserve the existing npu_probe's version-aware NPU proof contract:
    # OpenVINO 2026.3.1 returns a string, other releases return a vector.
    value = compiled.get_property("EXECUTION_DEVICES")
    devices = [value] if isinstance(value, str) else value
    if (
        not isinstance(devices, (list, tuple))
        or not devices
        or any(not isinstance(d, str) or not re.fullmatch(r"NPU(?:\.\d+)?", d) for d in devices)
    ):
        raise ValueError("compiled model is not direct NPU execution")
    return compiled, list(devices)


def build_model(ov, layers):
    ops = ov.opset13
    parameter = ops.parameter([256, 64], np.float32, name="heldout_digits")
    current = parameter
    for index, (weights, bias) in enumerate(layers):
        current = ops.add(
            ops.matmul(current, ops.constant(weights), False, False),
            ops.constant(bias),
        )
        if index < len(layers) - 1:
            current = ops.relu(current)
    return ov.Model([current], [parameter], "frozen_digits_mlp_64_256_256_10")


def device_property(core, name):
    try:
        return str(core.get_property("NPU", name))
    except RuntimeError as exc:
        return "unavailable: " + str(exc)


def run(fixture_path, rounds, *, emit_compiled_artifact=False):
    if rounds < 1:
        raise ValueError("positive fixed work required")
    preparation_started_at = time.time()
    fixture, inputs, reference, layers, fixture_sha = load_fixture(fixture_path)
    import openvino as ov

    core = ov.Core()
    model = build_model(ov, layers)
    compilation_started_at = time.time()
    before = time.perf_counter()
    compiled, devices = compile_on_npu(core, model)
    compilation_seconds = time.perf_counter() - before
    compilation_finished_at = time.time()
    artifact_error = None
    compiled_sha = None
    try:
        blob = compiled.export_model().getvalue()
        compiled_sha = hashlib.sha256(blob).hexdigest()
        if emit_compiled_artifact:
            print(
                "POOL_COMPILED_ARTIFACT "
                + json.dumps({"sha256": compiled_sha, "data": base64.b64encode(blob).decode()}),
                flush=True,
            )
    except RuntimeError as exc:
        artifact_error = str(exc)
    request = compiled.create_infer_request()
    for _ in range(3):
        request.infer({0: inputs})
    samples = []
    started = time.time()
    for index in range(rounds):
        before = time.perf_counter()
        request.infer({0: inputs})
        samples.append(time.perf_counter() - before)
        if (index + 1) % 256 == 0:
            print(
                "POOL_PROGRESS " + json.dumps(dict(rounds=index + 1, elapsed_seconds=sum(samples))),
                flush=True,
            )
    finished = time.time()
    output = np.asarray(request.get_output_tensor(0).data, dtype=np.float32).copy()
    quality = measure_quality(output, reference, fixture["labels"])
    model_identity = {
        "model": fixture["model"],
        "precision": fixture["precision"],
        "layers": [
            {
                "inputs": x["inputs"],
                "outputs": x["outputs"],
                "weights": x["weights"]["sha256"],
                "bias": x["bias"]["sha256"],
            }
            for x in fixture["layers"]
        ],
    }
    model_sha = hashlib.sha256(
        json.dumps(model_identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    source_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return dict(
        measured=True,
        evidence_kind="hardware",
        outcome="COMPLETED" if quality["quality_passed"] else "QUALITY_FAILED",
        fixture_sha256=fixture_sha,
        input_sha256=fixture["input"]["sha256"],
        model_sha256=model_sha,
        source_sha256=source_sha,
        kernel_sha256=source_sha,
        compiled_sha256=compiled_sha,
        compiled_artifact_export_error=artifact_error,
        accelerator_model=device_property(core, "FULL_DEVICE_NAME"),
        arch={"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()],
        runtime_versions=dict(
            openvino=ov.get_version(),
            numpy=np.__version__,
            python=platform.python_version(),
            host_kernel=platform.release(),
            npu_driver_version_raw=device_property(core, "NPU_DRIVER_VERSION"),
            npu_compiler_version_raw=device_property(core, "NPU_COMPILER_VERSION"),
        ),
        execution_devices=devices,
        precision="fp32-model-and-io; Intel NPU internal FP16",
        graph_operations="static MatMul/Add/ReLU equivalent to original frozen dense layers",
        batch_size=256,
        rounds=rounds,
        images=256 * rounds,
        warmup=3,
        preparation_started_at=preparation_started_at,
        compilation_started_at=compilation_started_at,
        compilation_finished_at=compilation_finished_at,
        compilation_seconds=compilation_seconds,
        compute_started_at=started,
        compute_finished_at=finished,
        elapsed_seconds=sum(samples),
        round_seconds=samples,
        measurement_boundary="blocking host-input-to-NPU-output infer; three warmups excluded",
        output_sha256=hashlib.sha256(output.astype("<f4").tobytes()).hexdigest(),
        tensor_allocation_bytes=inputs.nbytes + sum(w.nbytes + b.nbytes for w, b in layers),
        sensor=[],
        sensor_errors=["NPU utilization/power/allocated device memory not measured"],
        **quality,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--rounds", type=int, required=True)
    parser.add_argument("--emit-compiled-artifact", action="store_true")
    args = parser.parse_args()
    report = run(args.fixture, args.rounds, emit_compiled_artifact=args.emit_compiled_artifact)
    print("POOL_INFERENCE_RESULT " + json.dumps(report, allow_nan=False), flush=True)
    raise SystemExit(0 if report["quality_passed"] else 2)
