"""Bounded native quota/account negatives; run as a lab controller operator.

Private config contains projects a/b with user/account/partition/node/gres/qos.
An unexpected acceptance is recorded and canceled by that same Linux user.
This does not test queue order, API ownership or GPU execution.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


def verify(config, report):
    if os.geteuid() != 0 or set(config["projects"]) != {"a", "b"}:
        raise ValueError("explicit two-project lab controller operator required")
    with report.open("x"):
        pass
    report.chmod(0o600)
    state = {"status": "INCOMPLETE", "requests": []}

    def save():
        report.write_text(json.dumps(state, indent=2) + "\n")

    for label, scope in config["projects"].items():
        for key in ("user", "account", "partition", "node", "qos"):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", scope[key]):
                raise ValueError("unsafe scope identifier")
        if not re.fullmatch(r"gpu:[A-Za-z0-9_]+", scope["gres"]):
            raise ValueError("explicit typed physical GPU required")
        other = config["projects"]["b" if label == "a" else "a"]
        cases = [
            ("cpu", {"cpus-per-task": "2"}, "QOSMaxCpuPerJobLimit"),
            ("memory", {"mem": "2048M"}, "QOSMaxMemoryPerJob"),
            ("qos", {"qos": "normal"}, "Invalid qos specification"),
            ("account", {"account": other["account"]}, "Invalid account"),
        ]
        for kind, overrides, expected in cases:
            options = {
                "job-name": "ra-e6-limit-" + label + "-" + kind,
                "partition": scope["partition"],
                "account": scope["account"],
                "qos": scope["qos"],
                "nodelist": scope["node"],
                "nodes": "1",
                "ntasks": "1",
                "cpus-per-task": "1",
                "mem": "1024M",
                "gres": scope["gres"] + ":1",
                "time": "1",
                "chdir": "/",
                "output": "/dev/null",
                **overrides,
            }
            entry = {"project": label, "case": kind, "intent": options}
            state["requests"].append(entry)
            save()
            prefix = ["/usr/sbin/runuser", "-u", scope["user"], "--"]
            command = prefix + ["/usr/bin/sbatch", "--parsable"]
            command += ["--" + key + "=" + value for key, value in options.items()]
            result = subprocess.run(
                command + ["--wrap=/bin/true"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            entry.update(exit=result.returncode, stdout=result.stdout, stderr=result.stderr)
            save()
            if result.returncode == 0:
                job = result.stdout.strip().split(";")[0]
                if not re.fullmatch(r"[1-9][0-9]*", job):
                    raise ValueError("inspect unexpected native receipt")
                entry["unexpected_job_id"] = job
                save()
                cancel = subprocess.run(
                    prefix + ["/usr/bin/scancel", job], capture_output=True, text=True, timeout=20
                )
                entry["cleanup_exit"] = cancel.returncode
                save()
                raise ValueError("native policy accepted an out-of-scope request; inspect saved ID")
            if expected.lower() not in result.stderr.lower():
                raise ValueError("rejection was not the required native policy boundary")
            entry["native_policy_rejection_verified"] = True
            save()
    state["status"] = "PASS"
    save()
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = verify(json.loads(args.config.read_text()), args.report)
    print("Native quota/account rejections verified:", len(result["requests"]))
