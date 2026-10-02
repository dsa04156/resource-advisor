"""Pure fixture/gate regressions; not simulated Hailo performance evidence."""

import importlib.util
from pathlib import Path

import pytest

from resource_advisor.hailo_qualification import load_fixture, quality_gates


def test_reference_agreement_does_not_override_failed_absolute_accuracy():
    labels = [0] * 100
    reference = [0] * 64 + [1] * 36
    verdict = quality_gates(reference, labels, reference)
    assert verdict["reference_agreement"] == 1
    assert verdict["accuracy"] == 0.64
    assert verdict["qualified"] is False
    assert verdict["checks"]["minimum_accuracy_0_75"] is False


def test_accuracy_alone_does_not_qualify_different_reference_behavior():
    verdict = quality_gates([0] * 100, [0] * 100, [0] * 89 + [1] * 11)
    assert verdict["accuracy"] == 1
    assert verdict["qualified"] is False


def test_accuracy_loss_gate_is_independent_and_has_exact_boundary():
    assert quality_gates([0] * 95 + [1] * 5, [0] * 100, [0] * 100)["qualified"]
    verdict = quality_gates([0] * 94 + [1] * 6, [0] * 100, [0] * 100)
    assert verdict["reference_agreement"] == 0.94
    assert verdict["checks"]["maximum_accuracy_loss_0_05"] is False


@pytest.mark.parametrize("prediction", [-1, 1000, 1.0, True, "1"])
def test_class_indices_are_bounded_integers(prediction):
    with pytest.raises(ValueError, match="class index"):
        quality_gates([prediction], [1], [1])


def test_missing_predictions_cannot_be_reported_as_accuracy():
    with pytest.raises(ValueError, match="per input"):
        quality_gates([], [], [])
    with pytest.raises(ValueError, match="per input"):
        quality_gates([1], [1, 2], [1])


def test_changed_manifest_is_rejected_before_array_loading(tmp_path):
    (tmp_path / "manifest.json").write_text('{"untrusted": true}')
    with pytest.raises(ValueError, match="manifest digest mismatch"):
        load_fixture(tmp_path, "0" * 64)


def test_selection_is_output_independent_balanced_and_order_invariant():
    path = Path(__file__).parents[1] / "examples/prepare_hailo_fixture.py"
    spec = importlib.util.spec_from_file_location("hailo_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = [f"dataset/val/{c}/image-{i}.JPEG" for c in module.LABELS for i in range(20)]
    selected = module.select_paths(paths)
    assert selected == module.select_paths(list(reversed(paths)))
    assert len(selected) == len(set(selected)) == 100
    assert all(sum(Path(p).parent.name == c for p in selected) == 10 for c in module.LABELS)
    with pytest.raises(ValueError, match="classes"):
        module.select_paths(paths + ["dataset/val/foreign/image.JPEG"])
