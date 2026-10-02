"""Standard-library native runtime guard, installed and pinned by the operator.

This detects changed qualified inputs. It is not a sandbox or protection against
a privileged writer racing verification: runtime files must be operator-owned.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path


class RuntimeMismatch(RuntimeError):
    pass


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def verify(manifest_path, expected_manifest, expected_environment, command):
    with Path(manifest_path).open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or digest(raw) != expected_manifest:
        raise RuntimeMismatch("runtime manifest digest mismatch")
    manifest = json.loads(raw)
    if manifest.get("schema_version") != "v1" or manifest.get("kind") != "native":
        raise RuntimeMismatch("unsupported runtime manifest")
    if manifest.get("environment_digest") != expected_environment:
        raise RuntimeMismatch("qualified environment mismatch")
    if manifest.get("architecture") != platform.machine():
        raise RuntimeMismatch("runtime architecture mismatch")
    if list(command) not in manifest.get("commands", []):
        raise RuntimeMismatch("command is not qualified")
    files = manifest.get("files", {})
    if not files or command[0] not in files:
        raise RuntimeMismatch("qualified executable file hash is required")
    for name, expected in files.items():
        path = Path(name)
        if not path.is_absolute() or not path.is_file():
            raise RuntimeMismatch("qualified file missing")
        with path.open("rb") as handle:
            observed = "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
        if observed != expected:
            raise RuntimeMismatch("qualified file changed")
    # Preserve scheduling/device visibility and job identities, not ambient
    # Python/library/preload settings or the operator's credentials.
    env = {
        k: v
        for k, v in os.environ.items()
        if (k.startswith("SLURM_") and k != "SLURM_JWT")
        or k
        in {
            "RA_JOB_ID",
            "RA_ATTEMPT_ID",
            "RA_EPOCH",
            "RA_WORKLOAD_SIGNATURE",
            "RA_CONTEXT_SIGNATURE",
            "RA_CONTEXT_JSON",
            "RA_WORK_UNITS",
            "RA_INPUT_SHAPE",
            "RA_PRECISION",
            "RA_SEED",
            "RA_EXECUTION_MODE",
            "RA_ARTIFACT_PREFIX",
        }
        or k in {"CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES"}
    }
    env.update({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    for name, value in manifest.get("environment", {}).items():
        if name.startswith(("RA_", "SLURM_")) or name.endswith("VISIBLE_DEVICES"):
            raise RuntimeMismatch("manifest must not override scheduler identity or devices")
        env[name] = value
    for probe in manifest.get("probes", []):
        result = subprocess.run(
            probe["command"], capture_output=True, env=env, timeout=15, check=False
        )
        if result.returncode or digest(result.stdout) != probe["stdout_digest"]:
            raise RuntimeMismatch("runtime probe differs from qualification")
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--environment-digest", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("qualified workload command required")
    env = verify(args.manifest, args.manifest_digest, args.environment_digest, command)
    os.chdir("/")
    os.execve(command[0], command, env)


if __name__ == "__main__":
    main()
