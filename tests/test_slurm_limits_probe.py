import errno
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

path = Path(__file__).parents[1] / "examples/slurm_limits_probe.py"
spec = importlib.util.spec_from_file_location("slurm_limits_probe", path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_inherited_limit_uses_tightest_ancestor_not_unlimited_step(tmp_path):
    root = tmp_path / "cgroup"
    step = root / "job" / "step"
    step.mkdir(parents=True)
    (root / "memory.max").write_text("max\n")
    (step.parent / "memory.max").write_text(str(64 * 1024**2))
    (step / "memory.max").write_text("max\n")
    assert probe.effective_limit(step, root, "memory.max") == 64 * 1024**2
    (step / "memory.max").write_text(str(32 * 1024**2))
    assert probe.effective_limit(step, root, "memory.max") == 32 * 1024**2


def test_absent_or_unlimited_hierarchy_cannot_authorize_memory_probe(tmp_path):
    step = tmp_path / "step"
    step.mkdir()
    assert probe.effective_limit(step, tmp_path, "memory.max") is None
    (step / "memory.max").write_text("max\n")
    assert probe.effective_limit(step, tmp_path, "memory.max") is None
    (step / "memory.swap.max").write_text("0\n")
    assert probe.effective_limit(step, tmp_path, "memory.swap.max") == 0


def test_invalid_or_outside_hierarchy_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="escaped"):
        probe.effective_limit(tmp_path.parent, tmp_path, "memory.max")
    (tmp_path / "memory.max").write_text("-1")
    with pytest.raises(RuntimeError, match="invalid"):
        probe.effective_limit(tmp_path, tmp_path, "memory.max")


def test_memory_probe_refuses_to_run_outside_slurm(monkeypatch):
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.setattr("sys.argv", ["probe", "--mode", "oom"])
    with pytest.raises(RuntimeError, match="real Slurm allocation"):
        probe.main()


@pytest.mark.parametrize("failure", [None, "missing", "accessible", "unrelated-cuda"])
def test_negative_requires_each_device_denied_and_a_relevant_cuda_failure(
    monkeypatch, tmp_path, capsys, failure
):
    source = tmp_path / "cuda.py"
    source.write_bytes(b"# scheduler-output double; no CUDA execution\n")
    paths = ["/dev/nvidia0", "/dev/nvgpu/igpu0/ctrl", "/dev/dri/renderD128"]
    args = [
        "probe",
        "--mode",
        "gpu-denied",
        "--cuda-probe",
        str(source),
        "--cuda-probe-sha256",
        hashlib.sha256(source.read_bytes()).hexdigest(),
    ]
    for name in paths:
        args.extend(["--denied-device", name])
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setenv("SLURM_JOB_ID", "test")
    monkeypatch.setattr(probe.os, "sched_getaffinity", lambda _: {0})
    monkeypatch.setattr(
        probe, "effective_limit", lambda _p, _r, f: 256 * 1024**2 if f == "memory.max" else 0
    )
    opened = []

    def fake_open(name, flags):
        opened.append(name)
        if name == paths[1] and failure == "accessible":
            return 123
        error = errno.ENOENT if name == paths[1] and failure == "missing" else errno.EPERM
        raise OSError(error, "test device outcome")

    def fake_cuda(*args, **kwargs):
        if failure == "unrelated-cuda":
            raise RuntimeError("GPU kernel result differs from reference")
        raise RuntimeError("cuInit failed with CUDA error 100")

    monkeypatch.setattr(probe.os, "open", fake_open)
    monkeypatch.setattr(probe.os, "close", lambda _fd: None)
    monkeypatch.setattr(probe.runpy, "run_path", fake_cuda)
    if failure:
        with pytest.raises(RuntimeError):
            probe.main()
        assert "RA_GPU_DENIED " not in capsys.readouterr().out
    else:
        probe.main()
        assert opened == paths
        payload = capsys.readouterr().out.split("RA_GPU_DENIED ")[1]
        assert json.loads(payload)["devices"] == paths
