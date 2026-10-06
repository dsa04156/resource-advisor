"""Forced SSH gateway for a dedicated, unprivileged, single-project Slurm UID.

The operator owns this file, its scope and the SSH authorized_keys. This is a
command boundary, not a sandbox for jobs: Slurm/cgroups and Unix permissions
remain responsible for execution isolation. No shell evaluates request text.
"""

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

REF = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"
DIGEST = r"sha256:[a-f0-9]{64}"
ACCOUNTING_ENV = ["env", "TZ=UTC", "SLURM_TIME_FORMAT=%Y-%m-%dT%H:%M:%S%z"]
FIELDS = "JobIDRaw,JobName%100,Account%100,Partition%100"
ENV_KEYS = {
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


def batch(config, script):
    if len(script.encode()) > 65536 or not script.startswith("#!/bin/bash\n"):
        raise ValueError("bounded batch script required")
    lines = script.splitlines()
    directives, exports = {}, {}
    index = 1
    while index < len(lines) and lines[index].startswith("#SBATCH "):
        key, value = lines[index][8:].split("=", 1)
        if key in directives:
            raise ValueError("duplicate batch option")
        directives[key] = value
        index += 1
    if lines[index] != "set -euo pipefail":
        raise ValueError("unexpected batch body")
    index += 1
    while index < len(lines) and lines[index].startswith("export "):
        tokens = shlex.split(lines[index])
        if len(tokens) != 2 or tokens[0] != "export":
            raise ValueError("one literal export required")
        key, value = tokens[1].split("=", 1)
        if key not in ENV_KEYS or key in exports or "\x00" in value:
            raise ValueError("unqualified environment")
        exports[key] = value
        index += 1
    if set(exports) != ENV_KEYS:
        raise ValueError("complete job identity required")
    for key in ["RA_JOB_ID", "RA_ATTEMPT_ID"]:
        if not re.fullmatch(REF, exports[key]):
            raise ValueError("invalid identity")
    for key in ["RA_CONTEXT_SIGNATURE", "RA_WORKLOAD_SIGNATURE"]:
        if not re.fullmatch(DIGEST, exports[key]):
            raise ValueError("invalid signature")
    if not re.fullmatch(r"[1-9][0-9]*", exports["RA_EPOCH"]):
        raise ValueError("invalid epoch")
    context = json.loads(exports["RA_CONTEXT_JSON"])
    cpu_only = config.get("cpu_only", False)
    if type(cpu_only) is not bool:
        raise ValueError("cpu_only must be an operator-configured boolean")
    if cpu_only and (
        config.get("gres") is not None
        or context.get("allocation_mode") != "cpu_only"
        or context.get("memory_model") != "host"
    ):
        raise ValueError("CPU-only scope cannot request accelerator resources")
    resources = {"host_cpu": 1, "host_memory_mib": 1024, "accelerator_count": 0 if cpu_only else 1}
    if context["resources"] != resources:
        raise ValueError("unqualified resource request")
    priority_qos = config.get("qos_by_priority", {})
    if (
        not isinstance(priority_qos, dict)
        or set(priority_qos) - {"normal", "high"}
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)
            for value in priority_qos.values()
        )
        or priority_qos.get("normal", config["qos"]) != config["qos"]
    ):
        raise ValueError("invalid configured priority QOS mapping")
    selected_qos = directives.get("--qos")
    if selected_qos not in {config["qos"], *priority_qos.values()}:
        raise ValueError("QOS outside configured priority scope")
    expected = {
        "--job-name": exports["RA_ATTEMPT_ID"],
        "--comment": "resource-advisor:" + exports["RA_JOB_ID"],
        "--partition": config["partition"],
        "--account": config["account"],
        "--qos": selected_qos,
        "--nodelist": config["node"],
        "--nodes": "1",
        "--ntasks": "1",
        "--chdir": "/",
        "--cpus-per-task": "1",
        "--mem": "1024M",
        "--time": directives.get("--time"),
        "--output": config["output_dir"] + "/" + exports["RA_ATTEMPT_ID"] + ".log",
        "--export": "NONE",
    }
    if not cpu_only:
        expected["--gres"] = config["gres"] + ":1"
    if directives != expected or directives["--time"] not in {"1", "2"}:
        raise ValueError("batch escaped configured scope")
    tail = "\n".join(lines[index:]) + "\n"
    bindings = config.get("native_bindings", [])
    if not any(
        b["environment_digest"] == context["environment_digest"] and b["tail"] == tail
        for b in bindings
    ):
        raise ValueError("native runtime is not enabled")
    # Re-quote data and rebuild the shell. Client substitutions/separators are
    # literal values; only the root-owned runtime tail can supply executable code.
    return (
        "#!/bin/bash\n"
        + "\n".join("#SBATCH " + k + "=" + v for k, v in expected.items())
        + "\nset -euo pipefail\n"
        + "\n".join("export " + k + "=" + shlex.quote(v) for k, v in exports.items())
        + "\n"
        + tail
    )


def plan(config, original, script=""):
    if not isinstance(original, str) or len(original) > 4096:
        raise ValueError("bounded command required")
    argv = shlex.split(original)
    if config["mode"] == "results":
        if len(argv) != 5 or argv[:4] != ["tail", "-c", "65537", "--"]:
            raise ValueError("result read only")
        path = Path(argv[4])
        if str(path.parent) != config["output_dir"] or not re.fullmatch(REF + r"\.log", path.name):
            raise ValueError("result path escaped scope")
        return "read", str(path)
    if config["mode"] != "controller":
        raise ValueError("unknown gateway mode")
    if argv == ["sbatch", "--parsable"]:
        return ["/usr/bin/sbatch", "--parsable"], batch(config, script)
    account, partition, user = (config[k] for k in ["account", "partition", "user"])
    queue = ["squeue", "--noheader", "--account", account, "--partition", partition]
    if argv in (queue + ["--format=%i|%j|%a|%P|%T"], queue + ["--format=%i|%j|%a|%P|%T|%r"]) or (
        len(argv) == len(queue) + 3
        and argv[: len(queue)] == queue
        and argv[-3] == "--name"
        and re.fullmatch(REF, argv[-2])
        and argv[-1] == "--format=%i|%j|%a|%P"
    ):
        return ["/usr/bin/squeue", *argv[1:], "--user", user], None
    if argv[:3] == ACCOUNTING_ENV:
        args = argv[3:]
        prefix = ["sacct", "--noheader", "--parsable2"]
        by_job = prefix + [
            "--duplicates",
            "--accounts",
            account,
            "--partition",
            partition,
            "--jobs",
        ]
        formats = {
            "--format=" + FIELDS + ",State%64,ExitCode,Start,End,AllocTRES%200,Submit",
            "--format=" + FIELDS + ",NodeList%100,State%64,ExitCode",
        }
        valid = (
            len(args) == len(by_job) + 2
            and args[: len(by_job)] == by_job
            and re.fullmatch(r"[1-9][0-9]*", args[-2])
            and args[-1] in formats
        )
        if len(args) == 12 and args[:4] == prefix + ["--name"]:
            valid = (
                re.fullmatch(REF, args[4])
                and args[5:10] == ["--accounts", account, "--partition", partition, "--starttime"]
                and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", args[10])
                and args[11] == "--format=" + FIELDS
            )
        if valid:
            return ["/usr/bin/sacct", *args[1:], "--user", user], None
    cancel = ["scancel", "--ctld", "--account", account, "--partition", partition, "--name"]
    if (
        len(argv) == len(cancel) + 2
        and argv[: len(cancel)] == cancel
        and re.fullmatch(REF, argv[-2])
        and re.fullmatch(r"[1-9][0-9]*", argv[-1])
    ):
        return ["/usr/bin/scancel", *argv[1:], "--user", user], None
    raise ValueError("command outside execution scope")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
        script = sys.stdin.read(65537) if original == "sbatch --parsable" else ""
        command, data = plan(config, original, script)
        if command == "read":
            fd = os.open(data, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError("result must be an owned regular file")
                handle.seek(max(0, info.st_size - 65537))
                sys.stdout.buffer.write(handle.read(65537))
            return 0
        result = subprocess.run(
            command,
            input=data,
            text=True,
            capture_output=True,
            timeout=25,
            check=False,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "TZ": "UTC",
                "SLURM_TIME_FORMAT": "%Y-%m-%dT%H:%M:%S%z",
            },
        )
        if result.returncode:
            raise ValueError("scheduler rejected request")
        sys.stdout.write(result.stdout)
        return 0
    except (ValueError, KeyError, IndexError, TypeError, OSError, subprocess.SubprocessError):
        print("Slurm execution request rejected or unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
