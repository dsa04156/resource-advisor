"""Capture a PostgreSQL snapshot via its qualified Kubernetes database Pod.

RA_DATABASE_URL identifies the source. The dump uses the same database/role via
the Pod's already configured local authentication. No password is sent in argv.
Use a new private directory outside the checkout. Never publish its contents.
"""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

from resource_advisor.backup import exported_snapshot, file_digest
from resource_advisor.store import Store


def capture(store, directory, prefix, *, timeout=180):
    if directory.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("backup directory must be outside the public checkout")
    url = store.engine.url
    if any(
        not value or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", value)
        for value in (url.username, url.database)
    ):
        raise ValueError("explicit safe source database and role identifiers required")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    archive = directory / "metadata.dump"
    status = {"status": "INCOMPLETE"}
    started = time.monotonic()
    try:
        with exported_snapshot(store) as snapshot:
            command = [
                *prefix,
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--lock-wait-timeout=10s",
                "--snapshot=" + snapshot["snapshot"],
                "--username=" + url.username,
                "--dbname=" + url.database,
            ]
            with archive.open("xb") as output, (directory / "pg_dump.stderr").open("xb") as errors:
                archive.chmod(0o600)
                (directory / "pg_dump.stderr").chmod(0o600)
                process = subprocess.run(command, stdout=output, stderr=errors, timeout=timeout)
                status["dump_exit"] = process.returncode
                if process.returncode:
                    raise RuntimeError("pg_dump failed; inspect the retained private stderr")
                output.flush()
                os.fsync(output.fileno())
        with archive.open("rb") as stream:
            if stream.read(5) != b"PGDMP":
                raise ValueError("custom PostgreSQL archive header missing")
        manifest = {
            "schema_version": "postgres-snapshot-v1",
            "server_version": snapshot["server_version"],
            "captured_at": snapshot["captured_at"],
            "tables": snapshot["tables"],
            "archive": file_digest(archive),
            "capture_seconds": time.monotonic() - started,
            "semantics": "same exported read-only snapshot; subsequent source writes excluded",
        }
        path = directory / "manifest.json"
        with path.open("x") as output:
            path.chmod(0o600)
            json.dump(manifest, output, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        status["status"] = "CAPTURED_NOT_RESTORE_VERIFIED"
        return manifest
    finally:
        path = directory / "capture-status.json"
        path.write_text(json.dumps(status, indent=2) + "\n")
        path.chmod(0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    source = Store(os.environ["RA_DATABASE_URL"])
    try:
        manifest = capture(
            source,
            args.directory,
            ["kubectl", "--namespace=" + args.namespace, "exec", "pod/" + args.pod, "--"],
        )
        print(
            json.dumps(
                {
                    "status": "CAPTURED_NOT_RESTORE_VERIFIED",
                    "tables": manifest["tables"],
                    "archive": manifest["archive"],
                }
            )
        )
    finally:
        source.engine.dispose()
