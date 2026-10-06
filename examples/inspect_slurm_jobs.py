"""Read only the explicitly supplied native IDs on a qualified Slurm client.

Output contains private ownership/node evidence: keep reports outside Git.
Use a separately authorized operator observer, never researcher credentials.
"""

import argparse
import json
import os
import re
import subprocess


def inspect(ids):
    if not ids or len(ids) > 16 or any(not re.fullmatch(r"[1-9][0-9]*", x) for x in ids):
        raise ValueError("one to sixteen explicit numeric job IDs required")
    env = dict(os.environ, TZ="UTC", SLURM_TIME_FORMAT="%Y-%m-%dT%H:%M:%S%z")
    fields = [
        "JobIDRaw",
        "JobName%100",
        "User%100",
        "Account%100",
        "Partition%100",
        "QOS%100",
        "State%64",
        "Start",
        "End",
        "AllocTRES%200",
        "ExitCode",
        "Submit",
    ]
    raw = subprocess.check_output(
        ["sacct", "-nP", "--duplicates", "--jobs", ",".join(ids), "--format=" + ",".join(fields)],
        text=True,
        env=env,
        timeout=15,
    )
    records = {job: [] for job in ids}
    for line in raw.splitlines():
        values = line.split("|")
        if len(values) != len(fields):
            raise ValueError("unexpected native accounting schema")
        if values[0] in records:
            records[values[0]].append(
                dict(zip([f.split("%")[0] for f in fields], values, strict=True))
            )
    output = {}
    for job in ids:
        query = subprocess.run(
            ["scontrol", "show", "job", job, "-o"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        if query.returncode and "Invalid job id" not in query.stderr:
            raise ValueError("native observation unavailable")
        native = dict(re.findall(r"(?:^|\s)(\w+)=([^\s]+)", query.stdout))
        output[job] = {"native": native, "accounting": records[job]}
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", action="append", required=True)
    args = parser.parse_args()
    print(json.dumps(inspect(args.job)))
