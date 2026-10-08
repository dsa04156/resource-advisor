"""Software contract tests; no synthetic output qualifies physical NPU hardware."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest


def workload():
    path = Path(__file__).parents[1] / "examples/pool_inference_openvino.py"
    spec = importlib.util.spec_from_file_location("pool_inference_openvino", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nonpositive_work_is_rejected_before_loading_runtime(tmp_path):
    with pytest.raises(ValueError, match="positive fixed work"):
        workload().run(tmp_path / "absent.json", 0)


class ExternalCore:
    available_devices = ["CPU", "NPU"]

    def __init__(self, devices):
        self.devices = devices

    def compile_model(self, model, device_name, config):
        assert device_name == "NPU"
        return self

    def get_property(self, name):
        assert name == "EXECUTION_DEVICES"
        return self.devices


@pytest.mark.parametrize("devices", [["CPU"], ["NPU", "CPU"], ["AUTO"], []])
def test_compiler_cannot_claim_direct_npu_when_any_execution_device_is_not_npu(devices):
    with pytest.raises(ValueError, match="direct NPU"):
        workload().compile_on_npu(ExternalCore(devices), object())


def test_direct_npu_compilation_accepts_device_ordinal():
    compiled, devices = workload().compile_on_npu(ExternalCore(["NPU.0"]), object())
    assert compiled is not None
    assert devices == ["NPU.0"]


def test_openvino_2026_string_execution_property_is_preserved_as_one_npu():
    compiled, devices = workload().compile_on_npu(ExternalCore("NPU"), object())
    assert compiled is not None
    assert devices == ["NPU"]


@pytest.mark.parametrize("devices", ["NPU,CPU", ["NPU.fake"], ["NPU.0.CPU"]])
def test_execution_device_name_cannot_smuggle_non_npu_execution(devices):
    with pytest.raises(ValueError, match="direct NPU"):
        workload().compile_on_npu(ExternalCore(devices), object())


def test_preserved_quality_gate_reports_numerical_failure_even_with_identical_top1():
    report = workload().measure_quality(
        np.array([[3.0, 0.02]], dtype=np.float32),
        np.array([[3.0, 0.0]], dtype=np.float32),
        [0],
    )
    assert report["quality"] == 1.0
    assert report["accuracy"] == 1.0
    assert report["quality_passed"] is False
    assert report["max_absolute_error"] == pytest.approx(0.02)


def test_original_tolerance_allows_small_reference_relative_error():
    report = workload().measure_quality(
        np.array([[3.002, 0.002]], dtype=np.float32),
        np.array([[3.0, 0.0]], dtype=np.float32),
        [0],
    )
    assert report["quality_passed"] is True


def test_nonfinite_output_cannot_pass_quality_gate():
    report = workload().measure_quality(
        np.array([[float("nan"), 0.0]], dtype=np.float32),
        np.array([[3.0, 0.0]], dtype=np.float32),
        [0],
    )
    assert report["quality_passed"] is False
    assert report["max_absolute_error"] is None


def test_tensor_digest_is_checked_before_decoding():
    record = {"data": base64.b64encode(b"\0\0\x80?").decode(), "sha256": "0" * 64}
    with pytest.raises(ValueError, match="digest"):
        workload().decode_tensor(record, (1,))


def test_tensor_dimensions_cannot_silently_truncate_frozen_input():
    raw = b"\0\0\x80?"
    record = {"data": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}
    with pytest.raises(ValueError, match="shape"):
        workload().decode_tensor(record, (2,))


def test_other_model_cannot_be_reported_as_frozen_digits_mlp(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"model": "another model", "precision": "fp32", "layers": []}))
    with pytest.raises(ValueError, match="frozen Digits"):
        workload().load_fixture(path)
