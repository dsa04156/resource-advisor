"""Forced SSH gateway for a dedicated, unprivileged, single-project Slurm UID.

The operator owns this file, its scope and the SSH authorized_keys. This is a
command boundary, not a sandbox for jobs: Slurm/cgroups and Unix permissions
remain responsible for execution isolation. No shell evaluates request text.
"""

import argparse
import hashlib
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


TRAINING_KEY = "RA_TRAINING_BINDING"
LEGACY_RESOURCES = {"host_cpu": 1, "host_memory_mib": 1024}


def canonical_digest(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
        if key not in ENV_KEYS | {TRAINING_KEY} or key in exports or "\x00" in value:
            raise ValueError("unqualified environment")
        exports[key] = value
        index += 1
    if set(exports) - {TRAINING_KEY} != ENV_KEYS:
        raise ValueError("complete job identity required")
    if TRAINING_KEY in exports:
        # Only operator-enabled CPU host-copy training bindings may run natively.
        binding = json.loads(exports[TRAINING_KEY])
        if (
            not isinstance(binding, dict)
            or canonical_digest(binding) not in config.get("training_bindings", [])
            or binding.get("backend") != "slurm"
            or binding.get("device_class") != "cpu"
        ):
            raise ValueError("training binding is not enabled")
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
    allowed = config.get("resources", [dict(LEGACY_RESOURCES, accelerator_count=0 if cpu_only else 1)])
    if not isinstance(allowed, list) or any(
        not isinstance(r, dict)
        or set(r) != {"host_cpu", "host_memory_mib", "accelerator_count"}
        or type(r["host_cpu"]) is not int
        or type(r["host_memory_mib"]) is not int
        or r["accelerator_count"] != (0 if cpu_only else 1)
        for r in allowed
    ):
        raise ValueError("invalid configured resource scope")
    if context["resources"] not in allowed:
        raise ValueError("unqualified resource request")
    resources = context["resources"]
    max_minutes = config.get("max_minutes", 2)
    if type(max_minutes) is not int or not 1 <= max_minutes <= 60:
        raise ValueError("invalid configured time scope")
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
        "--cpus-per-task": str(int(resources["host_cpu"])),
        "--mem": f"{resources['host_memory_mib']}M",
        "--time": directives.get("--time"),
        "--output": config["output_dir"] + "/" + exports["RA_ATTEMPT_ID"] + ".log",
        "--export": "NONE",
    }
    if not cpu_only:
        expected["--gres"] = config["gres"] + ":1"
    if directives != expected or directives["--time"] not in {
        str(n) for n in range(1, max_minutes + 1)
    }:
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
        # 65537: terminal result envelope; 16384: bounded live progress tail.
        if len(argv) != 5 or argv[:2] != ["tail", "-c"] or argv[2:4] not in (
            ["65537", "--"],
            ["16384", "--"],
        ):
            raise ValueError("result read only")
        path = Path(argv[4])
        if str(path.parent) != config["output_dir"] or not re.fullmatch(REF + r"\.log", path.name):
            raise ValueError("result path escaped scope")
        return ("read" if argv[2] == "65537" else "read-progress"), str(path)
    if config["mode"] != "controller":
        raise ValueError("unknown gateway mode")
    if argv == ["sbatch", "--parsable"]:
        return ["/usr/bin/sbatch", "--parsable"], batch(config, script)
    account, partition, user = (config[k] for k in ["account", "partition", "user"])
    queue = ["squeue", "--noheader", "--account", account, "--partition", partition]
    if argv in (
        queue + ["--format=%i|%j|%a|%P|%T"],
        queue + ["--format=%i|%j|%a|%P|%T|%r"],
        queue + ["--format=%i|%j|%a|%P|%T|%r|%N"],
    ) or (
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
        # Reconciliation by attempt name; the --duplicates form keeps reused-ID history.
        history = args[3:4] == ["--duplicates"]
        by_name = args[4:] if history else args[3:]
        if args[:3] == prefix and len(by_name) == 9 and by_name[0] == "--name":
            valid = (
                re.fullmatch(REF, by_name[1])
                and by_name[2:7] == ["--accounts", account, "--partition", partition, "--starttime"]
                and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", by_name[7])
                and by_name[8] == "--format=" + FIELDS + (",Submit" if history else "")
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
        if isinstance(command, str):  # Bounded file reads; scheduler commands are argv lists.
            size = 65537 if command == "read" else 16384
            fd = os.open(data, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError("result must be an owned regular file")
                handle.seek(max(0, info.st_size - size))
                sys.stdout.buffer.write(handle.read(size))
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
            if command[0] == "/usr/bin/sbatch":
                # Only Slurm's own rejection lines; the worker allow-lists the reason.
                # A timeout or any other failure stays an uncertain submission.
                for line in result.stderr.splitlines():
                    if re.fullmatch(
                        r"sbatch: error: (?:Batch job submission failed: [ -~]{1,200}"
                        r"|(?:QOS|Assoc)[A-Za-z]{1,64})",
                        line,
                    ):
                        print(line, file=sys.stderr)
            raise ValueError("scheduler rejected request")
        sys.stdout.write(result.stdout)
        return 0
    except (ValueError, KeyError, IndexError, TypeError, OSError, subprocess.SubprocessError):
        print("Slurm execution request rejected or unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
