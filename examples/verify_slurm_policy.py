"""Bounded acceptance check for an already configured, dedicated Slurm lab account.

Run on a controller/client with existing authentication. This does not configure
the scheduler. It submits GPU jobs, cancels only its own IDs, and emits evidence.
The account must have an aggregate CPU limit of one, normal/high QOS with a one
CPU/1 GiB/two-minute per-job limit and DenyOnLimit, and a higher high-QOS priority.
The selected node needs one GPU and at least two CPUs. No other lab jobs may run.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("account", "partition", "node", "gres", "normal-qos", "high-qos", "probe"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    owned = []
    evidence = {"scope": "live scheduler policy; not model performance", "rejections": {}}
    prefix = "ra-policy-" + uuid.uuid4().hex[:10]

    def run(argv, *, script=None, check=True):
        return subprocess.run(
            argv,
            input=script,
            capture_output=True,
            text=True,
            check=check,
            timeout=15,
            env=dict(os.environ, LC_ALL="C", TZ="UTC", SLURM_TIME_FORMAT="%Y-%m-%dT%H:%M:%S%z"),
        )

    def submit(label, qos, seconds, extra=(), *, reject=False):
        cmd = [
            "sbatch",
            "--parsable",
            "--account=" + args.account,
            "--partition=" + args.partition,
            "--nodelist=" + args.node,
            "--nodes=1",
            "--ntasks=1",
            "--cpus-per-task=1",
            "--mem=512M",
            "--time=00:02:00",
            "--gres=" + args.gres,
            "--qos=" + qos,
            "--job-name=" + prefix + "-" + label,
            "--output=/tmp/ra-policy-%j.log",
            *extra,
        ]
        script = (
            "#!/bin/sh\nset -eu\n" + shlex.join(["python3", args.probe]) + f"\nsleep {seconds}\n"
        )
        result = run(cmd, script=script, check=False)
        match = re.fullmatch(r"(\d+)(?:;[\w.-]+)?", result.stdout.strip())
        if match:
            owned.append(match[1])
        if reject:
            evidence["rejections"][label] = {
                "exit": result.returncode,
                "stderr": result.stderr.strip(),
            }
            assert result.returncode != 0 and not match, f"{label} unexpectedly admitted"
            assert re.search(r"QOS|policy|limit", result.stderr, re.I), result.stderr
            return None
        assert result.returncode == 0 and match, result.stderr
        return match[1]

    def snapshot(jid):
        output = run(["scontrol", "show", "job", jid, "-o"]).stdout
        return dict(re.findall(r"(?:^|\s)(\w+)=([^\s]+)", output))

    def until(predicate, seconds=35):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(1)
        raise RuntimeError("acceptance observation deadline exceeded; inspect retained evidence")

    try:
        assert not run(["squeue", "-h", "--account=" + args.account]).stdout.strip(), (
            "account is busy"
        )
        submit("cpu-over-limit", args.normal_qos, 0, ("--cpus-per-task=2",), reject=True)
        submit("memory-over-limit", args.normal_qos, 0, ("--mem=2048M",), reject=True)
        submit("time-over-limit", args.normal_qos, 0, ("--time=00:03:00",), reject=True)
        submit("unauthorized-qos", "normal", 0, reject=True)
        blocker = submit("blocker", args.normal_qos, 90)
        until(lambda: snapshot(blocker).get("JobState") == "RUNNING")
        low = submit("normal", args.normal_qos, 3)
        high = submit("high", args.high_qos, 3)

        def pending():
            lo, hi = snapshot(low), snapshot(high)
            if lo.get("JobState") == hi.get("JobState") == "PENDING" and all(
                item.get("Reason") == "AssocGrpCpuLimit" for item in (lo, hi)
            ):
                return lo, hi
            return None

        lo, hi = until(pending)
        assert int(hi["Priority"]) > int(lo["Priority"]), "QOS priority is not effective"
        evidence["pending"] = {
            label: {k: item[k] for k in ("JobState", "Reason", "Priority", "QOS")}
            for label, item in (("normal", lo), ("high", hi))
        }
        factors = run(["sprio", "-j", low + "," + high, "-o", "%i|%Y|%Q"], check=False)
        # Some deployed versions return no factor entry for a just-submitted
        # pending job. Preserve that diagnostic; scontrol priority plus actual
        # start order remain the authoritative checks, not fabricated factors.
        evidence["priority_factors"] = {
            "exit": factors.returncode,
            "stdout": factors.stdout,
            "stderr": factors.stderr,
        }
        run(["scancel", blocker])
        until(lambda: all(snapshot(j).get("JobState") == "COMPLETED" for j in (low, high)), 50)
        out = run(
            [
                "sacct",
                "-nP",
                "-X",
                "--jobs",
                ",".join(owned),
                "--format=JobIDRaw,State,ExitCode,Submit,Start,End,AllocTRES%100,QOS",
            ]
        ).stdout
        evidence["accounting"] = out
        rows = {r.split("|")[0]: r.split("|") for r in out.splitlines()}
        assert rows[high][1:3] == rows[low][1:3] == ["COMPLETED", "0:0"]
        assert rows[high][4] < rows[low][4], "high-QOS job did not start before older normal job"
        assert all("gres/gpu=1" in rows[j][6] for j in (low, high))
        evidence["checks"] = {
            "normal_submitted_first": int(low) < int(high),
            "high_started_first": True,
            "gpu_reserved_for_both_completed_jobs": True,
            "quota_pending_reason_verified": True,
        }
        evidence["result"] = "PASS"
    finally:
        for jid in owned:
            run(["scancel", jid], check=False)
        evidence["owned_job_ids"] = owned
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
