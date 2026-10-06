import importlib.util
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
