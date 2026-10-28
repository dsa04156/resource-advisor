"""Write the native runtime manifest for the live CPU training runner (run in the guest as root).

The operator installs runtime_guard.py and live_training.py read-only under
--runtime-dir first. This records file digests, derives the environment digest
from them and prints only digests (no host data) for the Resource Advisor
registry and gateway configuration.
"""

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path


def digest(path):
    with Path(path).open("rb") as handle:
        return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()


def canonical(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default="/opt/ra/runtime")
    parser.add_argument("--python", default="/usr/bin/python3")
    parser.add_argument("--source-root", default="/srv/ra/training-sources")
    parser.add_argument("--work-dir", default="/srv/ra/work")
    parser.add_argument("--workload", action="append", required=True)
    args = parser.parse_args()
    runtime = Path(args.runtime_dir)
    runner = str(runtime / "live_training.py")
    files = {args.python: digest(args.python), runner: digest(runner)}
    for path in [runtime, *files]:
        info = os.stat(path)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit(f"{path} must be root-owned and not group/world writable")
    environment = canonical({"files": files, "architecture": platform.machine()})
    commands = [
        [args.python, "-I", runner, "--host-source", f"{args.source_root}/{ref}",
         "--host-work", args.work_dir]
        for ref in args.workload
    ]
    manifest = {
        "schema_version": "v1",
        "kind": "native",
        "environment_digest": environment,
        "architecture": platform.machine(),
        "commands": commands,
        "files": files,
    }
    path = runtime / "runtime.json"
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    path.chmod(0o644)
    print(
        json.dumps(
            {
                "environment_digest": environment,
                "manifest_path": str(path),
                "manifest_digest": digest(path),
                "guard_path": str(runtime / "runtime_guard.py"),
                "guard_digest": digest(runtime / "runtime_guard.py"),
                "runner_digest": files[runner],
                "python_digest": files[args.python],
                "python_version": platform.python_version(),
                "commands": commands,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
