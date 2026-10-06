import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).parents[1] / "examples/qualify_slurm_torch.py"
spec = importlib.util.spec_from_file_location("qualify_slurm_torch", path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_unlimited_step_retains_parent_memory_peak_and_swap_limit(tmp_path, monkeypatch):
    job = tmp_path / "job"
    step = job / "step"
    step.mkdir(parents=True)
    (step / "memory.max").write_text("max")
    (job / "memory.max").write_text(str(1024**3))
    (job / "memory.swap.max").write_text("0")
    (job / "memory.peak").write_text("123456789")
    monkeypatch.setattr(probe.os, "sched_getaffinity", lambda _: {2})
    assert probe.memory_boundary(step, tmp_path) == {
        "memory_max_bytes": 1024**3,
        "swap_max_bytes": 0,
        "memory_peak_bytes": 123456789,
        "cpu_affinity_count": 1,
    }
    (step / "memory.max").write_text(str(512 * 1024**2))
    result = probe.memory_boundary(step, tmp_path)
    assert result["memory_max_bytes"] == 512 * 1024**2
    assert result["memory_peak_bytes"] is None  # Never substitute a different cgroup's peak.


def test_missing_limits_and_escaped_membership_cannot_qualify(tmp_path):
    with pytest.raises(RuntimeError, match="finite"):
        probe.memory_boundary(tmp_path, tmp_path)
    with pytest.raises(RuntimeError, match="escaped"):
        probe.memory_boundary(tmp_path.parent, tmp_path)
    (tmp_path / "memory.max").write_text("-1")
    with pytest.raises(RuntimeError, match="negative"):
        probe.memory_boundary(tmp_path, tmp_path)


def test_qualification_rejects_non_slurm_execution_before_importing_torch(monkeypatch):
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.setattr("sys.argv", ["probe", "--expected-torch", "x", "--expected-cuda", "x"])
    with pytest.raises(RuntimeError, match="Slurm allocation"):
        probe.main()
