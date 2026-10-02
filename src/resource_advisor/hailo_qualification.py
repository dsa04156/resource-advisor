"""Bounded Hailo-8 ResNet18 qualification; no CPU inference fallback.

This command produces a qualification report, not a platform ExecutionResult.
The original ONNX CPU reference is prepared independently before this command.
"""

import argparse
import hashlib
import json
import platform
import resource
import statistics
import time
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def quality_gates(predictions, labels, reference):
    if not predictions or len(predictions) != len(labels) or len(labels) != len(reference):
        raise ValueError("one prediction, label and reference required per input")
    if any(
        type(v) is not int or not 0 <= v < 1000
        for seq in (predictions, labels, reference)
        for v in seq
    ):
        raise ValueError("invalid ImageNet class index")
    size = len(labels)
    accuracy = sum(a == b for a, b in zip(predictions, labels, strict=True)) / size
    reference_accuracy = sum(a == b for a, b in zip(reference, labels, strict=True)) / size
    agreement = sum(a == b for a, b in zip(predictions, reference, strict=True)) / size
    checks = {
        "minimum_accuracy_0_75": accuracy >= 0.75,
        "minimum_reference_agreement_0_90": agreement >= 0.90,
        "maximum_accuracy_loss_0_05": reference_accuracy - accuracy <= 0.05 + 1e-12,
    }
    return {
        "accuracy": accuracy,
        "reference_accuracy": reference_accuracy,
        "reference_agreement": agreement,
        "checks": checks,
        "qualified": all(checks.values()),
    }


def load_fixture(directory, manifest_digest):
    import numpy as np

    raw = (directory / "manifest.json").read_bytes()
    if digest(raw) != manifest_digest:
        raise ValueError("manifest digest mismatch")
    manifest = json.loads(raw)
    for name, key in [("inputs.npy", "inputs_sha256"), ("reference.npy", "reference_sha256")]:
        if digest((directory / name).read_bytes()) != manifest[key]:
            raise ValueError("fixture digest mismatch: " + name)
    inputs = np.load(directory / "inputs.npy", allow_pickle=False)
    reference = np.load(directory / "reference.npy", allow_pickle=False)
    if inputs.dtype != np.uint8 or inputs.shape != (100, 224, 224, 3):
        raise ValueError("fixture requires exactly 100 uint8 NHWC images")
    if reference.shape != (100, 1000) or not np.isfinite(reference).all():
        raise ValueError("invalid original model reference")
    rows = manifest["samples"]
    if len(rows) != 100 or len({r["path"] for r in rows}) != 100:
        raise ValueError("fixture must enumerate 100 distinct images")
    for row, tensor, scores in zip(rows, inputs, reference, strict=True):
        if digest(tensor.tobytes()) != row["tensor_sha256"]:
            raise ValueError("per-image tensor digest mismatch")
        if int(np.argmax(scores)) != row["reference_top1"]:
            raise ValueError("reference label mismatch")
    return manifest, inputs


def qualify(directory, hef_path, hef_digest, manifest_digest):
    import hailo_platform as hailo
    import numpy as np

    started = time.perf_counter()
    manifest, tensors = load_fixture(directory, manifest_digest)
    if digest(hef_path.read_bytes()) != hef_digest:
        raise ValueError("compiled model digest mismatch")
    if platform.machine() != "aarch64" or hailo.__version__ != "4.23.0":
        raise ValueError("unqualified host architecture or HailoRT version")
    driver = Path("/sys/module/hailo_pci/version").read_text().strip()
    if driver != "4.23.0":
        raise ValueError("unqualified PCIe driver")
    ids = hailo.Device.scan()
    if len(ids) != 1:
        raise ValueError("exactly one visible allocated Hailo device required")
    with hailo.Device(ids[0]) as device:
        identity = device.control.identify()
        architecture = str(identity.device_architecture)
        firmware = str(identity.firmware_version)
    if architecture != "HAILO8" or not firmware.startswith("4.23.0 "):
        raise ValueError("unqualified device or firmware")
    hef = hailo.HEF(str(hef_path))
    inputs, outputs = hef.get_input_vstream_infos(), hef.get_output_vstream_infos()
    if len(inputs) != 1 or tuple(inputs[0].shape) != (224, 224, 3):
        raise ValueError("unqualified HEF input")
    if len(outputs) != 1 or tuple(outputs[0].shape) not in {(1000,), (1, 1, 1000)}:
        raise ValueError("unqualified HEF output")
    params = hailo.VDevice.create_params()
    params.scheduling_algorithm = hailo.HailoSchedulingAlgorithm.NONE
    records, durations = [], []
    with hailo.VDevice(params, device_ids=ids) as target:
        configs = hailo.ConfigureParams.create_from_hef(
            hef, interface=hailo.HailoStreamInterface.PCIe
        )
        groups = target.configure(hef, configs)
        if len(groups) != 1:
            raise ValueError("one configured network group required")
        group = groups[0]
        input_params = hailo.InputVStreamParams.make_from_network_group(
            group, quantized=False, format_type=hailo.FormatType.FLOAT32
        )
        output_params = hailo.OutputVStreamParams.make_from_network_group(
            group, quantized=False, format_type=hailo.FormatType.FLOAT32
        )
        with hailo.InferVStreams(group, input_params, output_params) as pipeline:
            with group.activate(group.create_params()):
                for tensor in tensors[:4]:
                    pipeline.infer({inputs[0].name: tensor.astype(np.float32)[None]})
                for row, tensor in zip(manifest["samples"], tensors, strict=True):
                    feed = {inputs[0].name: tensor.astype(np.float32)[None]}
                    tick = time.perf_counter()
                    response = pipeline.infer(feed)
                    elapsed = time.perf_counter() - tick
                    scores = response[outputs[0].name]
                    if scores.shape != (1, *outputs[0].shape) or not np.isfinite(scores).all():
                        raise ValueError("invalid NPU model output")
                    durations.append(elapsed)
                    records.append(
                        {
                            "input_sha256": row["tensor_sha256"],
                            "output_sha256": digest(scores.tobytes()),
                            "label": row["label"],
                            "reference_top1": row["reference_top1"],
                            "prediction": int(np.argmax(scores)),
                            "elapsed_seconds": elapsed,
                        }
                    )
    quality = quality_gates(
        [r["prediction"] for r in records],
        [r["label"] for r in records],
        [r["reference_top1"] for r in records],
    )
    return {
        "schema_version": "v1",
        "kind": "hailo-model-qualification",
        "evidence_kind": "hardware",
        "device_count": 1,
        "architecture": architecture,
        "hailort": hailo.__version__,
        "driver": driver,
        "firmware": firmware,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "hef_sha256": hef_digest,
        "original_model_sha256": manifest["model_sha256"],
        "manifest_sha256": manifest_digest,
        "warmup_images": 4,
        "measured_images": len(records),
        "measurement_boundary": "synchronous-host-float32-NHWC-to-NPU-output",
        "elapsed_seconds": sum(durations),
        "latency_p50_ms": statistics.median(durations) * 1000,
        "latency_p95_ms": sorted(durations)[94] * 1000,
        "latency_p99_ms": sorted(durations)[98] * 1000,
        "throughput_images_per_second": len(records) / sum(durations),
        "host_process_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "npu_memory_utilization": None,
        "npu_utilization": None,
        "npu_power_watts": None,
        "process_seconds": time.perf_counter() - started,
        "quality": quality,
        "predictions": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--hef", type=Path, required=True)
    parser.add_argument("--hef-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    report = qualify(args.fixture, args.hef, args.hef_sha256, args.manifest_sha256)
    print("RESOURCE_ADVISOR_HAILO_REPORT " + json.dumps(report, allow_nan=False), flush=True)
    return 0 if report["quality"]["qualified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
