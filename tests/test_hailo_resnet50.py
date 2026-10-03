import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from resource_advisor.hailo_qualification import digest, load_fixture

ROOT = Path(__file__).parents[1]


def test_frozen_selection_excludes_both_inspected_cohorts(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "examples"))
    spec = importlib.util.spec_from_file_location(
        "resnet50", ROOT / "examples/prepare_hailo_resnet50.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifests = [
        ROOT / "docs/evidence" / name
        for name in ["hailo-input-manifest.json", "hailo-efficientformer-inputs.json"]
    ]
    previous = [row["path"] for p in manifests for row in json.loads(p.read_text())["samples"]]
    available = [
        f"imagenette2-160/val/{label}/unseen-{i}.JPEG" for label in module.LABELS for i in range(20)
    ]
    paths = previous + available
    selected = module.selected_inputs(paths, manifests)
    assert len(selected) == len(set(selected)) == 100
    assert not set(selected) & set(previous)
    assert selected == module.selected_inputs(list(reversed(paths)), manifests)
    assert all(sum(Path(p).parent.name == label for p in selected) == 10 for label in module.LABELS)
    with pytest.raises(ValueError, match="frozen prior"):
        module.selected_inputs(paths, manifests[::-1])
    with pytest.raises(ValueError, match="200 distinct"):
        module.selected_inputs(paths[1:], manifests)


@pytest.mark.parametrize(
    "declared,actual,valid",
    [
        (None, "uint8", True),
        ("float32", "float32", True),
        (None, "float32", False),
        ("float64", "float64", False),
        ("float32", "uint8", False),
    ],
)
def test_declared_fixture_pixel_format_is_enforced(tmp_path, monkeypatch, declared, actual, valid):
    tensor = np.zeros((224, 224, 3), dtype=actual)
    inputs = np.broadcast_to(tensor, (100, 224, 224, 3))
    reference = np.zeros((100, 1000), dtype=np.float32)
    (tmp_path / "inputs.npy").write_bytes(b"isolated-test-inputs")
    (tmp_path / "reference.npy").write_bytes(b"isolated-test-reference")
    manifest = {
        "inputs_sha256": digest(b"isolated-test-inputs"),
        "reference_sha256": digest(b"isolated-test-reference"),
        "samples": [
            {"path": f"fixture-{i}", "tensor_sha256": digest(tensor.tobytes()), "reference_top1": 0}
            for i in range(100)
        ],
    }
    if declared is not None:
        manifest["input_dtype"] = declared
    raw = json.dumps(manifest).encode()
    (tmp_path / "manifest.json").write_bytes(raw)
    monkeypatch.setattr(
        np, "load", lambda path, **kwargs: inputs if path.name == "inputs.npy" else reference
    )
    if valid:
        _, loaded = load_fixture(tmp_path, digest(raw))
        assert loaded is inputs
    else:
        with pytest.raises(ValueError, match="dtype|pixel format"):
            load_fixture(tmp_path, digest(raw))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 255.1])
def test_float_fixture_rejects_nonfinite_or_out_of_range_pixels(tmp_path, monkeypatch, value):
    inputs = np.broadcast_to(np.float32(value), (100, 224, 224, 3))
    (tmp_path / "inputs.npy").write_bytes(b"invalid-input")
    (tmp_path / "reference.npy").write_bytes(b"reference")
    manifest = {
        "input_dtype": "float32",
        "inputs_sha256": digest(b"invalid-input"),
        "reference_sha256": digest(b"reference"),
    }
    raw = json.dumps(manifest).encode()
    (tmp_path / "manifest.json").write_bytes(raw)
    monkeypatch.setattr(
        np,
        "load",
        lambda path, **kwargs: inputs if path.name == "inputs.npy" else np.zeros((100, 1000)),
    )
    with pytest.raises(ValueError, match="pixel format"):
        load_fixture(tmp_path, digest(raw))
