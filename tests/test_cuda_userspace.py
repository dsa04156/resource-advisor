import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

path = Path(__file__).parents[1] / "examples/prepare_cuda_userspace.py"
spec = importlib.util.spec_from_file_location("prepare_cuda_userspace", path)
preparer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparer)


def manifest(data):
    return [
        {
            "component": "cuda_cudart",
            "url": "https://developer.download.nvidia.com/compute/cuda/redist/"
            "cuda_cudart/linux-aarch64/cuda_cudart-test.tar.xz",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    ]


@pytest.mark.parametrize("mutation", ["driver", "host", "duplicate", "hash", "size"])
def test_manifest_rejects_unpinned_or_non_userspace_archive(mutation):
    value = manifest(b"archive")
    if mutation == "driver":
        value[0]["url"] = value[0]["url"].replace("/cuda_cudart/", "/nvidia_driver/")
    elif mutation == "host":
        value[0]["url"] = value[0]["url"].replace(
            "developer.download.nvidia.com", "example.invalid"
        )
    elif mutation == "duplicate":
        value *= 2
    elif mutation == "hash":
        value[0]["sha256"] = "unknown"
    else:
        value[0]["size"] = True
    with pytest.raises(ValueError):
        preparer.validate_manifest(value)


def test_verified_payload_stays_in_new_directory_and_cannot_be_replayed(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        file = tarfile.TarInfo("runtime/lib/example.so")
        file.size = 4
        archive.addfile(file, io.BytesIO(b"test"))
    data = buffer.getvalue()
    monkeypatch.setattr(preparer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preparer.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(preparer.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(data))
    output = tmp_path / "candidate"
    assert preparer.prepare(manifest(data), output)["exit"] == 0
    assert (output / "root/runtime/lib/example.so").read_bytes() == b"test"
    with pytest.raises(FileExistsError):
        preparer.prepare(manifest(data), output)
    bad = manifest(data)
    bad[0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        preparer.prepare(bad, tmp_path / "bad")
    assert not list((tmp_path / "bad/root").iterdir())
    assert json.loads((tmp_path / "bad/status.json").read_text())["exit"] == 1
