"""Freeze, dry-run, execute and analyze the bounded B0–B5 × E1–E8 experiment.

  freeze   --capacity capacity.json --seed N --out plan.json
  check    --plan plan.json                         (digest, budget, unsupported list)
  run      --plan plan.json --digest sha256:... [--profiling-profile REF] [--only E6]
           [--execute --api URL --token-env RA_TOKEN] > raw.jsonl
  analyze  --plan plan.json --raw raw.jsonl --out DIR (CSV + evidence manifest; no charts)
  report   --plan plan.json --analysis DIR --out DOCS_DIR (08-RESULTS.md, 10-SUMMARY.md)

Default is a dry run. --execute submits only through the authenticated Resource
Advisor API, inside the frozen budget, and cancels only the runner's own jobs.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

from resource_advisor.contracts import signature
from resource_advisor.hetero_experiment import (
    Runner,
    analyze,
    freeze_plan,
    render_reports,
    rerun_commands,
    scenario_status,
)


class Client:
    def __init__(self, api, token):
        self.api, self.token = api.rstrip("/") + "/api/v1/compute", token

    def request(self, method, path, body=None, idempotency_key=None):
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.api + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # Rejections (e.g. NEEDS_PROFILE) are outcomes to record, not crashes.
            return {"http_status": exc.code, "detail": exc.read().decode()[:2000]}

    def post(self, path, body, idempotency_key=None):
        return self.request("POST", path, body, idempotency_key)

    def get(self, path):
        return self.request("GET", path)


def write_jsonl(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("freeze")
    p.add_argument("--capacity", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("check")
    p.add_argument("--plan", required=True)
    p = sub.add_parser("run")
    p.add_argument("--plan", required=True)
    p.add_argument("--digest", required=True)
    p.add_argument("--only", action="append")
    p.add_argument(
        "--profiling-profile", help="run only the profiling phase under this legacy profile"
    )
    p.add_argument("--execute", action="store_true")
    p.add_argument("--api")
    p.add_argument("--token-env", default="RA_TOKEN")
    p.add_argument(
        "--capture-pods", action="store_true", help="record accelerator Pods before each cohort"
    )
    p = sub.add_parser("report")
    p.add_argument("--plan", required=True)
    p.add_argument("--analysis", required=True, help="directory written by analyze")
    p.add_argument("--out", required=True)
    p = sub.add_parser("analyze")
    p.add_argument("--plan", required=True)
    p.add_argument("--raw", required=True)
    p.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.command == "freeze":
        capacity = json.loads(Path(args.capacity).read_text())
        plan, digest = freeze_plan(capacity, seed=args.seed)
        Path(args.out).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        print(
            json.dumps({"plan": args.out, "digest": digest, "planned": plan["planned"]}, indent=2)
        )
        return
    plan = json.loads(Path(args.plan).read_text())
    if args.command == "check":
        print(
            json.dumps(
                {
                    "digest": signature(plan),
                    "planned": plan["planned"],
                    "budget": plan["budget"],
                    "scenarios": plan["scenarios"],
                    "rerun": rerun_commands(plan, args.plan),
                },
                indent=2,
            )
        )
        return
    if args.command == "run":
        client = None
        if args.execute:
            token = os.environ.get(args.token_env)
            if not args.api or not token:
                sys.exit("--execute requires --api and a token in the named environment variable")
            client = Client(args.api, token)
        capture = None
        if args.capture_pods:
            import subprocess

            from resource_advisor.contracts import now
            from resource_advisor.hetero_collect import accelerator_pods

            def capture():
                done = subprocess.run(
                    ["kubectl", "get", "pods", "-A", "-o", "json"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if done.returncode != 0:
                    return {"observed_at": now().isoformat(), "status": "CAPTURE_FAILED"}
                pods = accelerator_pods(json.loads(done.stdout))
                return {"observed_at": now().isoformat(), "status": "ok", "pods": pods}

        runner = Runner(plan, args.digest, client, write=write_jsonl, capture=capture)
        if args.profiling_profile:
            # Profiling only; evaluation starts in a later call after profiles are registered.
            runner.run_profiling(execute=args.execute, profile_ref=args.profiling_profile)
            return
        runner.run(execute=args.execute, only=set(args.only) if args.only else None)
        return
    if args.command == "report":
        folder = Path(args.analysis)
        results, summary = render_reports(
            plan,
            (folder / "results.csv").read_text(),
            (folder / "scenario-status.csv").read_text(),
        )
        out = Path(args.out)
        (out / "08-RESULTS.md").write_text(results)
        (out / "10-SUMMARY.md").write_text(summary)
        return
    raw = Path(args.raw)
    records = [json.loads(line) for line in raw.read_text().splitlines() if line.strip()]
    table = analyze(plan, records)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.csv").write_text(table)
    (out / "scenario-status.csv").write_text(scenario_status(plan))
    manifest = {
        "plan_digest": signature(plan),
        "files": {
            str(path): "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(args.plan), raw, out / "results.csv", out / "scenario-status.csv")
        },
        "note": "Charts are produced only from these live records; none without new measurements.",
    }
    (out / "evidence-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
