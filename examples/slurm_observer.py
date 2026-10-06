"""Root-owned forced-SSH command for one read-only Slurm inventory scope."""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

IDENTIFIER = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}"
NODE_FIELDS = {
    "name",
    "architecture",
    "operating_system",
    "version",
    "state",
    "cpus",
    "effective_cpus",
    "alloc_cpus",
    "real_memory",
    "specialized_memory",
    "alloc_memory",
    "tres",
    "tres_used",
    "gres",
    "gres_used",
    "gres_drained",
}
JOB_FIELDS = {"job_id", "account", "partition", "job_state"}


def observe(config, original, *, execute=subprocess.run):
    if set(config) != {"account", "partition", "nodes"}:
        raise ValueError("exact observer scope required")
    account, partition, nodes = (config[k] for k in ["account", "partition", "nodes"])
    if (
        not isinstance(nodes, list)
        or not 1 <= len(nodes) <= 100
        or any(
            not isinstance(v, str) or not re.fullmatch(IDENTIFIER, v)
            for v in [account, partition, *nodes]
        )
        or len(set(nodes)) != len(nodes)
        or not isinstance(original, str)
        or len(original) > 1024
    ):
        raise ValueError("invalid observer scope or command")
    requested = shlex.split(original)
    allowed = {
        ("scontrol", "--json=v0.0.42", "show", "nodes"): "nodes",
        ("squeue", "--json=v0.0.42", "--account=" + account, "--partition=" + partition): "jobs",
    }
    collection = allowed.get(tuple(requested))
    if collection is None:
        raise ValueError("command is outside the read-only observer scope")
    result = execute(
        ["/usr/bin/" + requested[0], *requested[1:]],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    if result.stderr.strip() or len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError("incomplete observer response")
    payload = json.loads(result.stdout)
    if (
        payload["meta"]["plugin"]["data_parser"] != "data_parser/v0.0.42"
        or payload["meta"]["slurm"]["release"] != "24.11.5"
        or not isinstance(payload[collection], list)
        or len(payload[collection]) > 10000
    ):
        raise ValueError("unqualified controller schema")
    rows = payload[collection]
    if collection == "nodes":
        rows = [r for r in rows if r["name"] in nodes]
        if len(rows) != len(nodes) or {r["name"] for r in rows} != set(nodes):
            raise ValueError("missing or ambiguous authorized node")
        fields = NODE_FIELDS
    else:
        if any(r["account"] != account or partition not in r["partition"].split(",") for r in rows):
            raise ValueError("queue escaped the authorized scope")
        fields = JOB_FIELDS
    return {
        "meta": {
            "plugin": {"data_parser": "data_parser/v0.0.42"},
            "slurm": {"release": "24.11.5"},
        },
        "errors": payload.get("errors", []),
        "warnings": payload.get("warnings", []),
        collection: [{k: v for k, v in r.items() if k in fields} for r in rows],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = observe(
            json.loads(args.config.read_text()), os.environ.get("SSH_ORIGINAL_COMMAND")
        )
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.SubprocessError):
        print("Slurm observer request rejected or unavailable", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
