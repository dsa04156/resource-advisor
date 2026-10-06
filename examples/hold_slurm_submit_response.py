#!/usr/bin/env python3
"""Qualification-only SSH shim for one accepted Slurm submit response.

Mount as /fault/ssh only in the isolated Slurm worker acceptance test. Set
RA_RECOVERY_ATTEMPT to the pre-created attempt, and prepend /fault to PATH.
The normal SSH binary remains /usr/bin/ssh. Other commands pass through.
Remove the mount/env after testing; never enable on ordinary workers.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def matching_submission(args, script, target):
    if not target or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", target):
        return False
    try:
        command = shlex.split(args[-1]) if args else []
    except ValueError:
        return False
    lines = script.splitlines()
    return (
        command == ["sbatch", "--parsable"]
        and lines.count("#SBATCH --job-name=" + target) == 1
        and sum(line.startswith("#SBATCH --job-name=") for line in lines) == 1
        and any(line.startswith("#SBATCH --comment=resource-advisor:") for line in lines)
    )


def main():
    args = sys.argv[1:]
    try:
        submitting = bool(args) and shlex.split(args[-1]) == ["sbatch", "--parsable"]
    except ValueError:
        submitting = False
    data = sys.stdin.buffer.read() if submitting else None
    script = data.decode("utf-8") if data is not None else ""
    target = os.environ.get("RA_RECOVERY_ATTEMPT")
    matches = matching_submission(args, script, target)
    if matches:
        with Path("/tmp/slurm-submit-attempts.jsonl").open("a") as counter:
            counter.write(json.dumps({"attempt": target}) + "\n")
    result = subprocess.run(["/usr/bin/ssh", *args], input=data, capture_output=True, check=False)
    output = result.stdout.decode("utf-8").strip()
    if matches and result.returncode == 0 and re.fullmatch(r"[0-9]+(?:;[A-Za-z0-9_.-]+)?", output):
        Path("/tmp/accepted-slurm-submit.json").write_text(
            json.dumps(
                {
                    "attempt": target,
                    "external_id": output.split(";", 1)[0],
                    "accepted_at": datetime.now(UTC).isoformat(),
                    "script_sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        )
        # Kill only the worker before its normal 30-second command timeout.
        time.sleep(60)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
