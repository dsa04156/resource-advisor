#!/usr/bin/env python
"""Qualification-only kubectl shim: hold one accepted Job creation response.

Mount as /fault/kubectl and prepend /fault to PATH only in an isolated worker
test. RA_RECOVERY_ATTEMPT must be the exact pre-created application attempt ID.
The real kubectl stays at /usr/local/bin/kubectl. Never enable for ordinary work.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    args = sys.argv[1:]
    data = sys.stdin.buffer.read() if "create" in args and "-" in args else None
    matches = False
    target = os.environ.get("RA_RECOVERY_ATTEMPT")
    if data and target:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            obj = {}
        matches = (
            isinstance(obj, dict)
            and obj.get("kind") == "Job"
            and obj.get("metadata", {}).get("name") == target
            and obj["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by")
            == "resource-advisor"
        )
    if matches:
        with Path("/tmp/create-attempts.txt").open("a") as attempts:
            attempts.write(obj["metadata"]["name"] + "\n")
    result = subprocess.run(
        ["/usr/local/bin/kubectl", *args], input=data, capture_output=True, check=False
    )
    if matches and result.returncode == 0:
        accepted = json.loads(result.stdout)
        Path("/tmp/accepted-submit.json").write_text(
            json.dumps(
                {
                    "name": accepted["metadata"]["name"],
                    "uid": accepted["metadata"]["uid"],
                }
            )
        )
        time.sleep(60)  # Controller is killed before its normal 30-second call timeout.
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
