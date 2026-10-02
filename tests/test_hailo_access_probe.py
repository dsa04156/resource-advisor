"""False-positive regressions for the lab probe, not hardware evidence."""

import errno
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_probe(monkeypatch, *, open_error, uid=10001):
    path = Path(__file__).parents[1] / "examples/hailo/device_access.py"
    spec = importlib.util.spec_from_file_location("device_access", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "hailo_platform", SimpleNamespace(__version__="4.23.0"))
    module.Path = lambda _: SimpleNamespace(read_text=lambda: "4.23.0", glob=lambda _: [])

    def open_device(*_):
        if open_error is not None:
            raise OSError(open_error, "probe failure")
        return 10

    module.os = SimpleNamespace(
        open=open_device, close=lambda _: None, geteuid=lambda: uid, O_RDWR=2, O_CLOEXEC=0
    )
    return module


@pytest.mark.parametrize("error", [errno.ENOENT, errno.EPERM, errno.EACCES])
def test_expected_device_denial_can_pass_for_non_root(monkeypatch, error):
    module = load_probe(monkeypatch, open_error=error)
    assert module.probe("denied")["passed"] is True


@pytest.mark.parametrize("error", [errno.EIO, errno.EMFILE, errno.EBUSY])
def test_device_failure_is_not_evidence_of_allocation_isolation(monkeypatch, error):
    module = load_probe(monkeypatch, open_error=error)
    result = module.probe("denied")
    assert result["passed"] is False
    assert result["errors"][0]["errno"] == error


def test_device_access_without_request_fails_the_check(monkeypatch):
    module = load_probe(monkeypatch, open_error=None)
    result = module.probe("denied")
    assert result["opened"] == ["/dev/hailo0"]
    assert result["passed"] is False


def test_root_execution_cannot_satisfy_non_root_probe(monkeypatch):
    module = load_probe(monkeypatch, open_error=errno.ENOENT, uid=0)
    assert module.probe("denied")["passed"] is False


def test_runtime_mismatch_is_not_accepted_as_device_denial(monkeypatch):
    module = load_probe(monkeypatch, open_error=errno.ENOENT)
    monkeypatch.setitem(sys.modules, "hailo_platform", SimpleNamespace(__version__="unqualified"))
    with pytest.raises(RuntimeError, match="qualified 4.23.0"):
        module.probe("denied")
