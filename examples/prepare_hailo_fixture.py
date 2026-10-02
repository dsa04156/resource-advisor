"""Prepare the preregistered 100-image Hailo qualification fixture and ONNX reference.

Requires numpy 2.2.6, Pillow 11.3.0 and onnxruntime 1.22.1 in an isolated environment.
Downloads are external; this validates the two public source archives by digest.
CPU reference execution is explicitly not an accelerator benchmark.
"""

import argparse
import hashlib
import io
import json
import time
from pathlib import Path
from tarfile import open as tar_open
from zipfile import ZipFile

ARCHIVES = {
    "model": "bc5776177ddad2b43f36c218ae06124b3dd67caaed9dd80c906c507f0c02a22f",
    "data": "64d0c4859f35a461889e0147755a999a48b49bf38a7e0f9bd27003f10db02fe5",
}
LABELS = {
    "n01440764": 0,
    "n02102040": 217,
    "n02979186": 482,
    "n03000684": 491,
    "n03028079": 497,
    "n03394916": 566,
    "n03417042": 569,
    "n03425413": 571,
    "n03445777": 574,
    "n03888257": 701,
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


EFFICIENTFORMER_ARCHIVE = "8372593b41a59f7e7d30ede776150c782bd3b7cb19073c30d9150c4826cdc8cf"
PREVIOUS_MANIFEST = "b96a400c1dd303d76119f69af6c444d13faeccd8026998345f8d3056950c4239"


def select_paths(paths, *, seed="20261003", excluded=()):
    if len(set(paths)) != len(paths):
        raise ValueError("validation paths must be distinct")
    if {Path(p).parent.name for p in paths} != LABELS.keys():
        raise ValueError("validation classes differ from the frozen plan")
    excluded = set(excluded)
    selected = []
    for label in sorted(LABELS):
        members = [p for p in paths if Path(p).parent.name == label and p not in excluded]
        if len(members) < 10:
            raise ValueError("class lacks ten distinct images")
        selected.extend(sorted(members, key=lambda p: digest((seed + ":" + p).encode()))[:10])
    return sorted(selected)


def prepare(model_archive, data_archive, output, recipe="resnet18", exclude_manifest=None):
    import numpy as np
    import onnxruntime as ort
    from PIL import Image
    from PIL import __version__ as pillow_version

    start = time.perf_counter()
    archives = dict(ARCHIVES)
    member, seed, interpolation = "resnet_v1_18.onnx", "20261003", Image.Resampling.BILINEAR
    excluded = []
    if recipe == "efficientformer-l1":
        if exclude_manifest is None or digest(exclude_manifest.read_bytes()) != PREVIOUS_MANIFEST:
            raise ValueError("the frozen previous manifest is required to exclude inspected images")
        excluded = [r["path"] for r in json.loads(exclude_manifest.read_text())["samples"]]
        if len(excluded) != 100 or len(set(excluded)) != 100:
            raise ValueError("exactly 100 previous distinct images required")
        archives["model"] = EFFICIENTFORMER_ARCHIVE
        member, seed = "efficientformer_l1.onnx", "20261003-efficientformer-l1"
        interpolation = Image.Resampling.BICUBIC
    elif recipe != "resnet18" or exclude_manifest is not None:
        raise ValueError("unqualified fixture recipe")
    for key, path in [("model", model_archive), ("data", data_archive)]:
        if digest(path.read_bytes()) != archives[key]:
            raise ValueError("source archive digest mismatch: " + key)
    output.mkdir(parents=True, exist_ok=False)
    with ZipFile(model_archive) as archive:
        model = archive.read(member)
    (output / "model.onnx").write_bytes(model)
    rows, tensors = [], []
    with tar_open(data_archive, "r:gz") as archive:
        paths = [
            m.name
            for m in archive.getmembers()
            if m.isfile() and "/val/" in m.name and m.name.lower().endswith(".jpeg")
        ]
        if len(paths) != 3925 or len(set(paths)) != len(paths):
            raise ValueError("unexpected validation population")
        if not set(excluded).issubset(paths):
            raise ValueError("excluded images do not belong to the frozen validation population")
        selected = select_paths(paths, seed=seed, excluded=excluded)
        for path in selected:
            raw = archive.extractfile(path).read()
            with Image.open(io.BytesIO(raw)) as image:
                image = image.convert("RGB")
                w, h = image.size
                w, h = int(w * 256 / min(w, h)), int(h * 256 / min(w, h))
                image = image.resize((w, h), interpolation)
                x, y = (w - 224) // 2, (h - 224) // 2
                tensor = np.asarray(image.crop((x, y, x + 224, y + 224)), dtype=np.uint8)
            assert tensor.shape == (224, 224, 3)
            tensors.append(tensor)
            rows.append(
                {
                    "path": path,
                    "image_sha256": digest(raw),
                    "tensor_sha256": digest(tensor.tobytes()),
                    "label": LABELS[Path(path).parent.name],
                }
            )
    batch = np.stack(tensors)
    np.save(output / "inputs.npy", batch, allow_pickle=False)
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    session = ort.InferenceSession(model, sess_options=options, providers=["CPUExecutionProvider"])
    input_info = session.get_inputs()
    if len(input_info) != 1 or input_info[0].shape != [1, 3, 224, 224]:
        raise ValueError("unqualified ONNX input")
    mean = np.asarray([123.675, 116.28, 103.53], dtype=np.float32)
    std = np.asarray([58.395, 57.12, 57.375], dtype=np.float32)
    scores = []
    reference_start = time.perf_counter()
    for row, tensor in zip(rows, batch, strict=True):
        feed = np.transpose((tensor.astype(np.float32) - mean) / std, (2, 0, 1))[None]
        outputs = session.run(None, {input_info[0].name: feed})
        if len(outputs) != 1 or outputs[0].shape != (1, 1000) or not np.isfinite(outputs[0]).all():
            raise ValueError("unqualified ONNX output")
        scores.append(outputs[0][0])
        row["reference_top1"] = int(np.argmax(outputs[0][0]))
    reference_seconds = time.perf_counter() - reference_start
    np.save(output / "reference.npy", np.stack(scores), allow_pickle=False)
    report = {
        "schema_version": "v1",
        "kind": f"imagenette100-{recipe}-qualification",
        "seed": 20261003 if recipe == "resnet18" else seed,
        "archives": archives,
        "model_sha256": digest(model),
        "inputs_sha256": digest((output / "inputs.npy").read_bytes()),
        "reference_sha256": digest((output / "reference.npy").read_bytes()),
        "numpy": np.__version__,
        "pillow": pillow_version,
        "onnxruntime": ort.__version__,
        "reference_provider": "CPUExecutionProvider",
        "population_size": len(paths),
        "samples": rows,
        "reference_accuracy": sum(r["reference_top1"] == r["label"] for r in rows) / len(rows),
        "reference_seconds": reference_seconds,
        "preparation_seconds": time.perf_counter() - start,
    }
    if recipe == "efficientformer-l1":
        report["excluded_manifest_sha256"] = PREVIOUS_MANIFEST
        report["excluded_images"] = len(excluded)
        report["resize"] = "Pillow bicubic shortest-side-256 center-crop-224"
    (output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-archive", type=Path, required=True)
    parser.add_argument("--data-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipe", choices=["resnet18", "efficientformer-l1"], default="resnet18")
    parser.add_argument("--exclude-manifest", type=Path)
    args = parser.parse_args()
    result = prepare(
        args.model_archive, args.data_archive, args.output, args.recipe, args.exclude_manifest
    )
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}))
