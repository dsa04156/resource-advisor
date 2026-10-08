"""Bounded, opt-in NPU examples, not arbitrary user-code profiling.

Standalone Python 3.7+ for the legacy RK3399Pro runtime. Device implementations
are imported only after binding validation. SDK calls are blocking. No CPU
execution fallback is accepted. Mobilint repeatability is NOT task accuracy.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import re
import resource
import statistics
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROUNDS = 10
SEED = 20261008
BOUNDARY = "blocking-host-input-to-NPU-output; memory=host-process-peak-RSS"
MOBILINT_MODEL = "aa158a35735a876cc9f9220f9967b711b8e49ce5de49126f0597e994c6b543da"


def digest(value):
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def file_digest(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def npu_execution_devices(value):
    # Direct NPU compilation returns a string in OpenVINO 2026.3.1, whereas
    # other plugins/versions use a vector. Iterating the string splits "NPU".
    devices = [value] if isinstance(value, str) else value
    if (
        not isinstance(devices, (list, tuple))
        or not devices
        or any(not isinstance(d, str) or not re.fullmatch(r"NPU(?:\.\d+)?", d) for d in devices)
    ):
        raise ValueError("Intel execution device is not exclusively NPU")
    return list(devices)


def validate_binding(binding, env):
    identity, context = binding["identity"], binding["context"]
    if (
        digest(identity) != env["RA_WORKLOAD_SIGNATURE"]
        or json.loads(env["RA_CONTEXT_JSON"]) != context
        or json.loads(env["RA_INPUT_SHAPE"]) != identity["input_shape"]
        or int(env["RA_WORK_UNITS"]) != identity["work_units"]
        or env["RA_PRECISION"] != identity["precision"]
        or int(env["RA_SEED"]) != identity["seed"]
        or env["RA_EXECUTION_MODE"] not in {"observe", "fixed"}
        or identity["seed"] != SEED
        or identity["work_units"] != ROUNDS
        or identity["measurement_boundary"] != BOUNDARY
        or context["resources"] != {"host_cpu": 1, "host_memory_mib": 1024, "accelerator_count": 1}
        or context["parameters"]
        or context["allocation_mode"] != "physical_device"
        or identity["model_digest"] != binding["model_digest"]
        or context["accelerator_model"] != binding["accelerator_model"]
    ):
        raise ValueError("submitted NPU contract differs from immutable binding")
    # Context signature includes the runtime variant; the server independently
    # verifies it. Never reconstruct a weaker signature from the context alone.


def mobilint():
    import importlib.metadata

    import numpy as np
    import qbruntime as qb

    if importlib.metadata.version("mobilint-qb-runtime") != "1.4.0":
        raise ValueError("qualified qbruntime 1.4.0 required")
    path = Path("/models/style_candy.mxq")
    if file_digest(path) != "sha256:" + MOBILINT_MODEL:
        raise ValueError("compiled Mobilint artifact digest mismatch")
    config = qb.ModelConfig()
    target = qb.CoreId(qb.Cluster.Cluster0, qb.Core.Core0)
    if not config.set_single_core_mode(core_ids=[target]):
        raise ValueError("single-core allocation refused")
    accelerator = qb.Accelerator(0)
    model = qb.Model(str(path), config)
    try:
        model.launch(accelerator)
        cores = [str(c) for c in model.get_target_cores()]
        shape = tuple(model.get_model_input_shape()[0])
        if cores != [str(target)] or shape != (224, 344, 3):
            raise ValueError("unqualified Mobilint core/shape")
        data = np.random.default_rng(SEED).uniform(-1, 1, shape).astype(np.float32)
        reference = np.array(model.infer([data])[0], copy=True)
        samples, checksums, agreements = [], [], []
        for _ in range(ROUNDS):
            start = time.perf_counter()
            output = np.array(model.infer([data])[0], copy=True)
            samples.append(time.perf_counter() - start)
            if output.shape != shape or not np.isfinite(output).all():
                raise ValueError("finite matching Mobilint output required")
            agreements.append(float(np.mean(output == reference)))
            checksums.append(hashlib.sha256(output.tobytes()).hexdigest())
        if not np.isfinite(reference).all():
            raise ValueError("finite reference output required")
        return {
            "runtime": "mobilint-candy",
            "accelerator_model": "ARIES2",
            "arch": "amd64",
            "model_digest": file_digest(path),
            "input_shape": list(shape),
            "precision": "mxq-float32-io",
            "runtime_versions": {
                "qbruntime": "1.4.0",
                "numpy": np.__version__,
                "python": platform.python_version(),
            },
            "input_digest": "sha256:" + hashlib.sha256(data.tobytes()).hexdigest(),
            "output_digests": checksums,
            "target_cores": cores,
            "samples_seconds": samples,
            "quality_value": min(agreements),
            "quality_metric": "finite_output_repeat_element_agreement",
            "task_accuracy": None,
            "quality_scope": "runtime-repeatability-only",
            "reference_output_digest": "sha256:" + hashlib.sha256(reference.tobytes()).hexdigest(),
        }
    finally:
        model.dispose()


def intel_conv():
    import tempfile

    import numpy as np
    import openvino as ov
    from openvino import opset13 as ops

    core = ov.Core()
    if "NPU" not in core.available_devices:
        raise ValueError("Intel NPU is not available; CPU fallback forbidden")
    data = np.random.RandomState(SEED).uniform(0, 1, (1, 3, 16, 16)).astype(np.float32)
    weights = np.arange(12, dtype=np.float32).reshape(4, 3, 1, 1) / 32
    bias = np.array([-0.5, -0.25, 0, 0.25], dtype=np.float32)
    spec = {
        "opset": 13,
        "input": [1, 3, 16, 16],
        "weights": weights.tolist(),
        "bias": bias.tolist(),
        "operations": ["conv1x1", "bias", "relu", "spatial_mean"],
        "absolute_tolerance": 0.002,
    }
    parameter = ops.parameter(data.shape, np.float32, name="input")
    conv = ops.convolution(parameter, ops.constant(weights), [1, 1], [0, 0], [0, 0], [1, 1])
    output = ops.reduce_mean(
        ops.relu(ops.add(conv, ops.constant(bias.reshape(1, 4, 1, 1)))),
        ops.constant(np.array([2, 3], dtype=np.int64)),
        False,
    )
    model = ov.Model([output], [parameter], "resource-advisor-fixed-conv-v1")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "model.xml"
        ov.serialize(model, str(path), str(path.with_suffix(".bin")))
        artifact = digest({"xml": file_digest(path), "bin": file_digest(path.with_suffix(".bin"))})
    start = time.perf_counter()
    compiled = core.compile_model(model, "NPU")
    compilation_seconds = time.perf_counter() - start
    devices = npu_execution_devices(compiled.get_property("EXECUTION_DEVICES"))
    expected = (
        np.maximum(
            np.tensordot(weights[:, :, 0, 0], data[0], axes=([1], [0])) + bias[:, None, None], 0
        )
        .mean(axis=(1, 2))
        .reshape(1, 4)
    )
    request = compiled.create_infer_request()
    request.infer({0: data})  # Separate warmup; synchronous completion.
    samples, outputs, errors = [], [], []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        request.infer({0: data})
        actual = np.array(request.get_output_tensor().data, copy=True)
        samples.append(time.perf_counter() - start)
        if actual.shape != (1, 4) or not np.isfinite(actual).all():
            raise ValueError("finite Intel CNN outputs required")
        outputs.append(actual.tolist())
        errors.append(float(np.max(np.abs(actual - expected))))
    return {
        "runtime": "intel-conv",
        "accelerator_model": "Intel-NPU-3720",
        "arch": "amd64",
        "model_digest": digest(spec),
        "serialized_model_digest": artifact,
        "input_shape": list(data.shape),
        "precision": "fp32-io-npu-default-internal",
        "runtime_versions": {
            "openvino": ov.__version__,
            "npu_driver_version_raw": str(core.get_property("NPU", "NPU_DRIVER_VERSION")),
            "npu_compiler_version_raw": str(compiled.get_property("NPU_COMPILER_VERSION")),
            "npu_compiler_type": str(compiled.get_property("NPU_COMPILER_TYPE")),
            "numpy": np.__version__,
            "python": platform.python_version(),
        },
        "input_digest": "sha256:" + hashlib.sha256(data.tobytes()).hexdigest(),
        "model_spec": spec,
        "outputs": outputs,
        "reference_output": expected.tolist(),
        "maximum_absolute_errors": errors,
        "absolute_tolerance": 0.002,
        "execution_devices": devices,
        "device_full_name": core.get_property("NPU", "FULL_DEVICE_NAME"),
        "compilation_seconds": compilation_seconds,
        "samples_seconds": samples,
        "quality_value": 1.0 if max(errors) <= 0.002 else 0.0,
        "quality_metric": "all_output_elements_within_absolute_tolerance_0.002",
        "task_accuracy": None,
        "quality_scope": "generated-CNN-numerical-reference",
    }


@contextmanager
def rknn_session(command, sdk_factory, launch=subprocess.Popen):
    """Always stop the USB helper, including constructor/release failures."""
    proxy = launch(command) if command is not None else None
    sdk = None
    try:
        sdk = sdk_factory()
        yield sdk
    finally:
        try:
            if sdk is not None:
                sdk.release()
        finally:
            if proxy is not None and proxy.poll() is None:
                proxy.terminate()
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait(timeout=5)


def rockchip_resnet18():
    import numpy as np
    from rknnlite.api import RKNNLite

    root = Path("/opt/rockchip")
    model = root / "resnet_18.rknn"
    model_digest = "sha256:a7363451bde07a8915f9ce08bbe874cfc2db812b424505184efbf9cce6c0ad3c"
    if file_digest(model) != model_digest:
        raise ValueError("official RKNN artifact digest mismatch")
    data = np.load(str(root / "input.npy"), allow_pickle=False)
    input_digest = "sha256:" + hashlib.sha256(data.tobytes()).hexdigest()
    if (
        data.shape != (224, 224, 3)
        or data.dtype != np.uint8
        or input_digest != "sha256:f36e0734e1f08d3ef7d380ee568fee2daa12b6c0e26d7446da9278ccb66e35f2"
    ):
        raise ValueError("official RGB input tensor digest mismatch")
    # The helper is confined to this Pod and its allocated USB device. The
    # operator must rule out an active host proxy before starting this runner.
    transport_path = root / "transport.json"
    transport = (
        json.loads(transport_path.read_text())
        if transport_path.is_file()
        else {"mode": "pod-owned"}
    )
    if transport["mode"] not in {"pod-owned", "host-persistent"}:
        raise ValueError("unqualified USB transport ownership")
    command = [str(root / "bin/npu_transfer_proxy")] if transport["mode"] == "pod-owned" else None
    with rknn_session(command, lambda: RKNNLite(verbose=False)) as rknn:
        time.sleep(0.5)
        if rknn.load_rknn(str(model)) != 0 or rknn.init_runtime(target="rk3399pro") != 0:
            raise ValueError("RK3399Pro hardware initialization failed; no CPU fallback")
        version = str(rknn.get_sdk_version())
        rknn.inference(inputs=[data])
        samples, predictions, probabilities = [], [], []
        for _ in range(ROUNDS):
            start = time.perf_counter()
            outputs = rknn.inference(inputs=[data])
            samples.append(time.perf_counter() - start)
            if not outputs:
                raise ValueError("complete RKNN output required")
            output = np.asarray(outputs[0]).reshape(-1)
            if output.shape != (1000,) or not np.isfinite(output).all():
                raise ValueError("finite 1000-class RKNN output required")
            prediction = int(np.argmax(output))
            predictions.append(prediction)
            probabilities.append(float(output[prediction]))
        return {
            "runtime": "rockchip-resnet18",
            "accelerator_model": "RK3399Pro",
            "arch": "arm64",
            "model_digest": model_digest,
            "compiled_artifact_digest": model_digest,
            "input_shape": list(data.shape),
            "precision": "rknn-compiled-uint8-io",
            "runtime_versions": {
                "rknn_toolkit_lite": re.search(r"API:\s*([\d.]+)", version).group(1),
                "transport_mode": transport["mode"],
                "transport_proxy_sha256": transport.get(
                    "proxy_sha256", file_digest(root / "bin/npu_transfer_proxy")
                ),
                "sdk": version,
                "numpy": np.__version__,
                "python": platform.python_version(),
            },
            "input_digest": input_digest,
            "samples_seconds": samples,
            "predictions": predictions,
            "top1_values": probabilities,
            "expected_class": 812,
            "quality_value": sum(p == 812 for p in predictions) / ROUNDS,
            "quality_metric": "official_space_shuttle_top1_agreement",
            "quality_scope": "one-official-example-not-dataset-accuracy",
            "task_accuracy": None,
        }


def qualify(runtime):
    providers = {
        "mobilint-candy": mobilint,
        "intel-conv": intel_conv,
        "rockchip-resnet18": rockchip_resnet18,
    }
    if runtime not in providers:
        raise ValueError("unqualified NPU runtime")
    started_at = datetime.now(timezone.utc).isoformat()
    report = providers[runtime]()
    versions = report["runtime_versions"]
    versions["host_kernel"] = platform.release()
    module = (
        "maccel"
        if runtime == "mobilint-candy"
        else "intel_vpu"
        if runtime == "intel-conv"
        else "not-host-accessible"
    )
    for field in ("version", "srcversion"):
        path = Path("/sys/module") / module / field
        versions["host_module_" + field] = (
            path.read_text().strip() if path.is_file() else "unobserved"
        )
    # No public firmware identity getter was qualified for these three devices.
    # Manual native execution remains possible; profile-based reuse must abstain.
    versions["profile_reuse"] = "blocked-unobserved-host-firmware"
    samples = report["samples_seconds"]
    if len(samples) != ROUNDS or any(not math.isfinite(t) or t <= 0 for t in samples):
        raise ValueError("positive measured sample times required")
    report.update(
        started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(),
        kind="npu-runtime-qualification",
        evidence_kind="hardware",
        seed=SEED,
        work_units=ROUNDS,
        warmup=1,
        measurement_boundary=BOUNDARY,
        elapsed_seconds=sum(samples),
        host_process_peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        npu_memory_mib=None,
        npu_power_watts=None,
        runner_digest=file_digest(__file__),
    )
    return report


def execute(binding, env=None):
    env = os.environ if env is None else env
    validate_binding(binding, env)
    report = qualify(binding["runtime"])
    identity, context = binding["identity"], binding["context"]
    if (
        report["model_digest"] != binding["model_digest"]
        or report["input_shape"] != identity["input_shape"]
        or report["precision"] != identity["precision"]
        or report["accelerator_model"] != context["accelerator_model"]
        or report["runtime_versions"] != context["runtime_versions"]
        or report["runner_digest"] != identity["code_digest"]
        or identity["dataset_version"] != "generated-fixed-input:" + report["input_digest"]
    ):
        raise ValueError("actual NPU runtime differs from qualified contract")
    valid = report["quality_value"] >= binding["minimum_quality"]
    result = {
        "schema_version": "v1",
        "job_id": env["RA_JOB_ID"],
        "attempt_id": env["RA_ATTEMPT_ID"],
        "epoch": int(env["RA_EPOCH"]),
        "workload_signature": env["RA_WORKLOAD_SIGNATURE"],
        "context_signature": env["RA_CONTEXT_SIGNATURE"],
        "outcome": "COMPLETED" if valid else "FAILED",
        "measured": True,
        "evidence_kind": "hardware",
        "error_code": None if valid else "NPU_QUALITY_GATE_FAILED",
        "measurements": None,
    }
    if valid:
        ordered = sorted(report["samples_seconds"])
        result["measurements"] = {
            "elapsed_seconds": float(report["elapsed_seconds"]),
            "peak_memory_mib": float(report["host_process_peak_rss_mib"]),
            "quality_value": float(report["quality_value"]),
            "sample_count": ROUNDS,
            "work_units": ROUNDS,
            "latency_p50_ms": statistics.median(ordered) * 1000,
            "latency_p95_ms": ordered[math.ceil(ROUNDS * 0.95) - 1] * 1000,
            "latency_p99_ms": ordered[math.ceil(ROUNDS * 0.99) - 1] * 1000,
            "throughput": ROUNDS / report["elapsed_seconds"],
            "gpu_utilization": None,
            "power_watts": None,
            "temperature_celsius": None,
        }
    print("RESOURCE_ADVISOR_NPU_REPORT " + json.dumps(report, allow_nan=False), flush=True)
    print(
        "RESOURCE_ADVISOR_RESULT "
        + json.dumps({"result": result, "digest": digest(result)}, allow_nan=False),
        flush=True,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=["mobilint-candy", "intel-conv", "rockchip-resnet18"])
    parser.add_argument("--binding", type=Path)
    args = parser.parse_args()
    if bool(args.binding) == bool(args.runtime):
        parser.error("select qualification runtime or immutable binding")
    if args.binding:
        execute(json.loads(args.binding.read_text()))
    else:
        print(
            "RESOURCE_ADVISOR_NPU_REPORT " + json.dumps(qualify(args.runtime), allow_nan=False),
            flush=True,
        )


if __name__ == "__main__":
    main()
