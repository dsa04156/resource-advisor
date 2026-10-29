"""Joint GPU-student / Mobilint-teacher KD trainer: refusal paths before any work.

These are CPU contract tests with a stand-in teacher; they are not hardware proof.
"""

import subprocess
import sys
import threading

import numpy as np
import pytest
from test_vision_training import environ

from resource_advisor import vision_joint_training as vj
from resource_advisor.mobilint_teacher import make_server

MXQ = "48b1ca4e669ac9d293148a748c06c352a7f542c5999f5f8eb2d1f5a31cf21e2d"
POD = "b46b90a5-340d-4ee6-95c8-d6dd4c8dd3a4"


class Echo:
    def infer(self, image):
        return np.zeros(1000, np.float32)


@pytest.fixture
def teacher():
    identity = {"model_sha256": MXQ, "runtime_version": "1.4.0", "pod_uid": POD}
    server = make_server(Echo(), "127.0.0.1", 0, identity)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def args(url, **change):
    values = {
        "model": "mobilenet_v3_small",
        "data": "/data",
        "dataset-digest": "sha256:" + "0" * 64,
        "initial-checkpoint-digest": "sha256:" + "1" * 64,
        "global-batch": 16,
        "steps": 20,
        "seed": 7,
        "teacher-url": url,
        "teacher-model-sha256": MXQ,
        "teacher-runtime-version": "1.4.0",
        "teacher-pod-uid": POD,
        "kd-config-digest": vj.kd_config_digest(MXQ, "1.4.0"),
    }
    values.update(change)
    return vj.parser().parse_args([f"--{k}={v}" for k, v in values.items()])


def test_preflight_accepts_the_signed_objective_and_pinned_teacher(teacher):
    _, world, identity = vj.preflight(args(teacher), environ())
    assert world == 1 and identity["pod_uid"] == POD


@pytest.mark.parametrize(
    "change, env, message",
    [
        ({"kd-config-digest": "sha256:" + "2" * 64}, {}, "KD config digest"),
        ({"teacher-pod-uid": "another-pod"}, {}, "pinned teacher"),
        ({"teacher-model-sha256": "0" * 64,
          "kd-config-digest": vj.kd_config_digest("0" * 64, "1.4.0")}, {}, "pinned teacher"),
        ({}, {"WORLD_SIZE": "2"}, "one device"),
        ({"strategy": "ddp"}, {}, "one device"),
        ({}, {"RA_WORK_UNITS": "50"}, "work units"),
    ],
)  # fmt: skip
def test_refuses_before_any_work_on_mismatch(teacher, change, env, message):
    with pytest.raises((RuntimeError, ValueError), match=message):
        vj.preflight(args(teacher, **change), environ(**env))


def test_unreachable_teacher_fails_without_emitting_a_result(capsys):
    with pytest.raises(OSError):
        vj.run(args("http://127.0.0.1:9"), environ())
    assert "RESOURCE_ADVISOR_RESULT" not in capsys.readouterr().out


def test_kd_objective_digest_is_distinct_and_printable_by_cli():
    digest = vj.kd_config_digest(MXQ, "1.4.0")
    assert digest != vj.kd_config_digest(MXQ, "1.5.0")
    out = subprocess.run(
        [sys.executable, "-m", "resource_advisor.vision_joint_training", "--model=resnet18",
         "--data=/d", "--dataset-digest=x", "--initial-checkpoint-digest=x", "--global-batch=16",
         "--steps=50", "--seed=1", "--teacher-url=http://t", f"--teacher-model-sha256={MXQ}",
         "--teacher-runtime-version=1.4.0", f"--teacher-pod-uid={POD}",
         "--print-kd-config-digest"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert out == digest


def test_manifest_classes_must_be_distinct_imagenet_indices():
    assert vj.class_indices({"classes": [185, 231]}) == [185, 231]
    for bad in ([], [185, 185], [1000], ["185"]):
        with pytest.raises(ValueError):
            vj.class_indices({"classes": bad})
