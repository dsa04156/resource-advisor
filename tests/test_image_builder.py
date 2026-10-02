import runpy
from pathlib import Path

import pytest


def test_image_builder_rejects_unverified_binary_before_build(tmp_path):
    build = runpy.run_path(str(Path(__file__).parents[1] / "examples/build_launcher.py"))["build"]
    binary = tmp_path / "kubectl"
    binary.write_bytes(b"modified-client")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="checksum mismatch"):
        build(
            "example.invalid/base@sha256:" + "a" * 64,
            "unused",
            output,
            "unused",
            kubectl=binary,
            kubectl_sha256="b" * 64,
        )
    assert not output.exists()


@pytest.mark.parametrize("problem", ["lock", "pin", "platform"])
def test_source_update_refuses_unmatched_runtime(tmp_path, problem):
    import hashlib
    import json

    repo = Path(__file__).parents[1]
    update = runpy.run_path(str(repo / "examples/update_service.py"))["update"]
    report = {
        "image": "example.invalid/runtime@sha256:" + "a" * 64,
        "lock_sha256": hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest(),
        "platform": "linux/amd64",
        "python": "3.11",
        "extras": [],
    }
    if problem == "lock":
        report["lock_sha256"] = "0" * 64
    elif problem == "pin":
        report["image"] = "example.invalid/runtime:latest"
    else:
        report["platform"] = "linux/arm64"
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        update(path, "unused", tmp_path / "build", "unused")
    assert not (tmp_path / "build").exists()


def test_source_layer_replaces_old_package_without_copying_dependencies(tmp_path, monkeypatch):
    import hashlib
    import json
    import subprocess
    import tarfile

    repo = Path(__file__).parents[1]
    update = runpy.run_path(str(repo / "examples/update_service.py"))["update"]
    image = "example.invalid/runtime@sha256:" + "a" * 64
    report = {
        "image": image,
        "lock_sha256": hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest(),
        "platform": "linux/amd64",
        "python": "3.11",
        "extras": ["artifacts", "optimizer"],
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(report))
    calls = []

    def mutate(command, **kwargs):
        calls.append(command)
        return "example.invalid/updated@sha256:" + "b" * 64

    monkeypatch.setattr(subprocess, "check_output", mutate)
    result = update(path, "example.invalid/updated:source", tmp_path / "build", "crane")
    assert len(calls) == 1 and calls[0][1:3] == ["mutate", image]
    assert result["extras"] == report["extras"]
    assert result["layer_bytes"] < 2_000_000
    with tarfile.open(tmp_path / "build/source-layer.tar") as archive:
        names = archive.getnames()
        prefix = "opt/resource-advisor/resource_advisor/"
        assert names[0] == prefix + ".wh..wh..opq"
        assert all(n.startswith(prefix) and "__pycache__" not in n for n in names)
        assert (
            archive.extractfile(prefix + "study.py").read()
            == (repo / "src/resource_advisor/study.py").read_bytes()
        )
        assert len(names) == len(set(names))
        assert all(info.uid == info.gid == info.mtime == 0 for info in archive.getmembers())
    assert (
        result["source_files"]["study.py"]
        == hashlib.sha256((repo / "src/resource_advisor/study.py").read_bytes()).hexdigest()
    )
