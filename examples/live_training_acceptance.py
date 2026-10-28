"""Live multi-request training acceptance helper (operator tool, not a scheduler).

register: builds the live CPU training contracts, registers them through the
          operator API and writes training source files + route snippets into a
          private output directory (never prints tokens).
submit:   submits one request through the same public API the console uses.
observe:  polls project-scoped job views and appends dated observations (JSONL).

Tokens are read from environment variables; the API CA from a file.
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from resource_advisor.contracts import signature
from resource_advisor.live_training_registry import build


def client(args, token_env):
    return httpx.Client(
        base_url=args.api.rstrip("/") + "/api/v1/compute",
        headers={"Authorization": "Bearer " + os.environ[token_env]},
        verify=args.ca or False,
        timeout=30,
    )


def register(args):
    plan = json.loads(Path(args.plan).read_text())
    out = Path(args.out)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    api = client(args, "RA_OPERATOR_TOKEN")
    routes = {}
    for item in plan["workloads"]:
        built = build(
            project=plan["project"],
            ref=item["ref"],
            backend=item["backend"],
            cluster=item["cluster"],
            node=item["node"],
            observed_at=datetime.fromisoformat(item["observed_at"]),
            valid_for_seconds=item["valid_for_seconds"],
            capability_evidence=item["capability_evidence"],
            validation_refs=item["validation_refs"],
            setting=item["setting"],
            resources=item["resources"],
            node_cpu=item["node_cpu"],
            node_memory_mib=item["node_memory_mib"],
            arch=item["arch"],
            environment_digest=item["environment_digest"],
            runtime_versions=item["runtime_versions"],
            command=item["command"],
            image=item.get("image"),
            execution=item.get("execution"),
        )
        for kind, path in [
            ("capability", "/capabilities"),
            ("variant", "/variants"),
            ("workload", "/workloads"),
            ("training_isolation", "/training-isolation"),
        ]:
            response = api.post(path, json=built[kind].model_dump(mode="json"))
            if response.status_code >= 400:
                sys.exit(f"{item['ref']} {kind} rejected: {response.status_code} {response.text[:300]}")
        files = out / item["ref"]
        files.mkdir(exist_ok=True)
        for name, data in built["files"].items():
            (files / name).write_bytes(data)
        binding = built["training_isolation"]
        routes[item["ref"]] = {
            "backend": item["backend"],
            "binding_digest": signature(binding),
            "checkpoint_digest": binding.checkpoint_digest,
            "input_digest": binding.input_digest,
            "workload_digest": signature(built["workload"]),
            "variant_ref": built["variant"].ref,
            "environment_digest": item["environment_digest"],
        }
    for profile in plan.get("scheduling_profiles", []):
        response = api.post("/scheduling-profiles", json=profile)
        if response.status_code >= 400:
            sys.exit(f"profile {profile['ref']} rejected: {response.status_code} {response.text[:300]}")
    (out / "registered.json").write_text(json.dumps(routes, indent=1, sort_keys=True))
    print(json.dumps({ref: r["binding_digest"] for ref, r in routes.items()}))


def submit(args):
    api = client(args, "RA_USER_TOKEN")
    body = {"workload_ref": args.workload, "mode": "observe"}
    if args.profile:
        body["scheduling_profile_ref"] = args.profile
    else:
        body["candidate_ref"] = "cpu"
    response = api.post("/jobs", json=body, headers={"Idempotency-Key": args.key})
    print(response.status_code, json.dumps(response.json()))


def observe(args):
    api = client(args, "RA_USER_TOKEN")
    deadline = time.monotonic() + args.seconds
    with Path(args.out).open("a") as sink:
        while time.monotonic() < deadline:
            for job_id in args.job:
                response = api.get(f"/jobs/{job_id}/view")
                row = {"read_at": datetime.now(UTC).isoformat(), "status": response.status_code}
                if response.status_code == 200:
                    view = response.json()
                    progress = view.get("live_progress") or {}
                    row.update(
                        {
                            key: view.get(key)
                            for key in (
                                "job_id",
                                "attempt_id",
                                "state",
                                "backend",
                                "cluster_ref",
                                "external_id",
                                "scheduler_reason",
                                "backend_observed_at",
                                "observed_placement",
                                "observed_allocation",
                                "admission",
                                "requested_resources",
                                "started_at",
                                "finished_at",
                                "error",
                                "progress_error",
                                "pipeline",
                            )
                        },
                        latest_progress=progress.get("latest"),
                        progress_observed_at=progress.get("observed_at"),
                        log_tail=progress.get("log_tail", [])[-5:],
                        result=view.get("result"),
                    )
                sink.write(json.dumps(row, sort_keys=True) + "\n")
                sink.flush()
            time.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--ca")
    sub = parser.add_subparsers(dest="action", required=True)
    r = sub.add_parser("register")
    r.add_argument("--plan", required=True)
    r.add_argument("--out", required=True)
    s = sub.add_parser("submit")
    s.add_argument("--workload", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--profile")
    o = sub.add_parser("observe")
    o.add_argument("--job", action="append", required=True)
    o.add_argument("--out", required=True)
    o.add_argument("--seconds", type=int, default=600)
    o.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    {"register": register, "submit": submit, "observe": observe}[args.action](args)


if __name__ == "__main__":
    main()
