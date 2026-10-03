"""Frozen ResNet-v1-50 TFLite reference preparation, never NPU performance evidence."""

import argparse
import json
import time
from pathlib import Path
from tarfile import open as tar_open
from zipfile import ZipFile

from prepare_hailo_fixture import ARCHIVES, LABELS, digest, select_paths

MODEL_ARCHIVE = "33eaf6896f9fb4876375d53eb12e32a90a12055d86c541f76361817d3aa8d5ad"
EXCLUDED_MANIFESTS = (
    "b96a400c1dd303d76119f69af6c444d13faeccd8026998345f8d3056950c4239",
    "5696247d5a65905dc2cc5421392957e2276749932394bc53451a484152208415",
)
SEED = "20261005-resnet-v1-50"


def selected_inputs(paths, manifests):
    import numpy as np

    if (
        len(manifests) != 2
        or tuple(digest(p.read_bytes()) for p in manifests) != EXCLUDED_MANIFESTS
    ):
        raise ValueError("both frozen prior manifests are required")
    excluded = [r["path"] for p in manifests for r in json.loads(p.read_text())["samples"]]
    if len(excluded) != 200 or len(set(excluded)) != 200 or not set(excluded).issubset(paths):
        raise ValueError("exactly 200 distinct prior validation inputs must be excluded")
    selected = select_paths(paths, seed=SEED, excluded=excluded)
    # Matches experimental-design.assign_factorial_runs on the sorted selected rows.
    order = np.argsort(np.random.default_rng(20261005).permutation(len(selected)) + 1)
    return [selected[int(index)] for index in order]


def prepare(model_archive, data_archive, manifests, output):
    import numpy as np
    import tensorflow as tf

    started = time.perf_counter()
    if tf.__version__ != "2.20.0" or np.__version__ != "2.2.6":
        raise ValueError("reference runtime must match the frozen versions")
    if (
        digest(model_archive.read_bytes()) != MODEL_ARCHIVE
        or digest(data_archive.read_bytes()) != ARCHIVES["data"]
    ):
        raise ValueError("source archive digest mismatch")
    output.mkdir(parents=True, exist_ok=False)
    with ZipFile(model_archive) as archive:
        model = archive.read("resnet_v1_50.tflite")
    (output / "model.tflite").write_bytes(model)
    rows, tensors = [], []
    with tar_open(data_archive, "r:gz") as archive:
        paths = [
            m.name
            for m in archive.getmembers()
            if m.isfile() and "/val/" in m.name and m.name.lower().endswith(".jpeg")
        ]
        if len(paths) != 3925 or len(set(paths)) != 3925:
            raise ValueError("unexpected validation population")
        selected = selected_inputs(paths, manifests)
        for path in selected:
            raw = archive.extractfile(path).read()
            image = tf.io.decode_jpeg(raw, channels=3)
            height, width = image.shape[:2]
            scale = np.float32(256) / np.float32(min(height, width))
            h, w = int(np.float32(height) * scale), int(np.float32(width) * scale)
            image = tf.image.resize(image[None], [h, w], method="bilinear", antialias=False)[0]
            tensor = image[
                (h - 224) // 2 : (h - 224) // 2 + 224, (w - 224) // 2 : (w - 224) // 2 + 224
            ].numpy()
            if (
                tensor.shape != (224, 224, 3)
                or tensor.dtype != np.float32
                or not np.isfinite(tensor).all()
            ):
                raise ValueError("invalid preprocessed input")
            tensors.append(tensor)
            rows.append(
                {
                    "path": path,
                    "image_sha256": digest(raw),
                    "tensor_sha256": digest(tensor.tobytes()),
                    "label": LABELS[Path(path).parent.name],
                }
            )
    np.save(output / "inputs.npy", np.stack(tensors), allow_pickle=False)
    interpreter = tf.lite.Interpreter(model_content=model, num_threads=1)
    interpreter.allocate_tensors()
    inputs, outputs = interpreter.get_input_details(), interpreter.get_output_details()
    if (
        len(inputs) != 1
        or inputs[0]["shape"].tolist() != [1, 224, 224, 3]
        or inputs[0]["dtype"] != np.float32
    ):
        raise ValueError("unqualified TFLite input")
    if (
        len(outputs) != 1
        or outputs[0]["shape"].tolist() != [1, 1000]
        or outputs[0]["dtype"] != np.float32
    ):
        raise ValueError("unqualified TFLite output")
    mean = np.asarray([123.68, 116.78, 103.94], dtype=np.float32)
    scores = []
    reference_started = time.perf_counter()
    for row, tensor in zip(rows, tensors, strict=True):
        interpreter.set_tensor(inputs[0]["index"], (tensor - mean)[None])
        interpreter.invoke()
        values = interpreter.get_tensor(outputs[0]["index"])[0]
        if not np.isfinite(values).all():
            raise ValueError("nonfinite reference output")
        scores.append(values)
        row["reference_top1"] = int(np.argmax(values))
    reference_seconds = time.perf_counter() - reference_started
    np.save(output / "reference.npy", np.stack(scores), allow_pickle=False)
    manifest = {
        "schema_version": "v1",
        "kind": "imagenette100-resnet50-tflite-qualification",
        "seed": SEED,
        "execution_order_seed": 20261005,
        "archives": {"model": MODEL_ARCHIVE, "data": ARCHIVES["data"]},
        "excluded_manifest_sha256": list(EXCLUDED_MANIFESTS),
        "excluded_images": 200,
        "model_sha256": digest(model),
        "inputs_sha256": digest((output / "inputs.npy").read_bytes()),
        "reference_sha256": digest((output / "reference.npy").read_bytes()),
        "input_dtype": "float32",
        "preprocessing": "tensorflow-2.20.0-rgb-bilinear-shortest256-center224-float32",
        "reference_provider": "TFLite CPU num_threads=1",
        "tensorflow": tf.__version__,
        "numpy": np.__version__,
        "population_size": 3925,
        "samples": rows,
        "reference_accuracy": sum(r["reference_top1"] == r["label"] for r in rows) / 100,
        "reference_seconds": reference_seconds,
        "preparation_seconds": time.perf_counter() - started,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model-archive", type=Path, required=True)
    p.add_argument("--data-archive", type=Path, required=True)
    p.add_argument("--exclude-manifest", type=Path, action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = prepare(a.model_archive, a.data_archive, a.exclude_manifest, a.output)
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}))
