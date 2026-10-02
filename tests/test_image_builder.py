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
