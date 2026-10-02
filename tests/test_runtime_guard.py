import json
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from resource_advisor.runtime_guard import RuntimeMismatch, digest, verify


def sealed(tmp_path):
    script = tmp_path / "work.py"
    script.write_text("print('qualified')\n")
    command = [sys.executable, "-I", str(script)]
    manifest = {
        "schema_version": "v1",
        "kind": "native",
        "architecture": platform.machine(),
        "environment_digest": "sha256:" + "a" * 64,
        "commands": [command],
        "files": {name: digest(Path(name).read_bytes()) for name in [sys.executable, str(script)]},
        "environment": {},
        "probes": [],
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(manifest))
    return path, digest(path.read_bytes()), manifest, command


def test_guard_executes_qualified_command_and_strips_credentials(tmp_path, monkeypatch):
    path, checksum, manifest, command = sealed(tmp_path)
    monkeypatch.setenv("RA_API_TOKEN", "private-test-token")
    monkeypatch.setenv("SLURM_JWT", "private-test-token")
    monkeypatch.setenv("PYTHONPATH", "/unqualified")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("RA_SAMPLING_PLAN_JSON", '{"policy_ref":"qualified"}')
    monkeypatch.setenv("RA_THERMAL_POLICY_JSON", '{"provider":"nvml-brackets-v1"}')
    env = verify(path, checksum, manifest["environment_digest"], command)
    assert "RA_API_TOKEN" not in env and "SLURM_JWT" not in env and "PYTHONPATH" not in env
    assert env["CUDA_VISIBLE_DEVICES"] == "0"
    assert env["RA_SAMPLING_PLAN_JSON"] == '{"policy_ref":"qualified"}'
    assert env["RA_THERMAL_POLICY_JSON"] == '{"provider":"nvml-brackets-v1"}'
    guard = Path(__file__).parents[1] / "src/resource_advisor/runtime_guard.py"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(guard),
            "--manifest",
            str(path),
            "--manifest-digest",
            checksum,
            "--environment-digest",
            manifest["environment_digest"],
            "--",
            *command,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "qualified\n"


def test_modified_source_is_rejected_before_workload_execution(tmp_path):
    path, checksum, manifest, command = sealed(tmp_path)
    marker = tmp_path / "executed"
    Path(command[-1]).write_text(f"open({str(marker)!r}, 'w').write('bad')")
    with pytest.raises(RuntimeMismatch, match="file changed"):
        verify(path, checksum, manifest["environment_digest"], command)
    assert not marker.exists()


@pytest.mark.parametrize("change", ["manifest", "environment", "command", "architecture", "probe"])
def test_guard_rejects_qualification_mismatch(tmp_path, change):
    path, checksum, manifest, command = sealed(tmp_path)
    expected = manifest["environment_digest"]
    if change == "manifest":
        path.write_text(path.read_text() + " ")
    elif change == "environment":
        expected = "sha256:" + "b" * 64
    elif change == "command":
        command = [sys.executable, "-c", "print('unqualified')"]
    else:
        if change == "architecture":
            manifest["architecture"] = "different-architecture"
        else:
            manifest["probes"] = [
                {
                    "command": [sys.executable, "-I", "-c", "print('changed')"],
                    "stdout_digest": digest(b"qualified\n"),
                }
            ]
        path.write_text(json.dumps(manifest))
        checksum = digest(path.read_bytes())
    with pytest.raises(RuntimeMismatch):
        verify(path, checksum, expected, command)
