#!/usr/bin/env python3
"""Bounded integrated-operations trial against an existing Compute API.

Standard library only. It submits a frozen cohort with fixed idempotency keys,
replays each key once, records operator fault marks, observes lifecycle until
every attempt is terminal or explicitly unresolved, then independently
recalculates usage from raw ledger rows. It never injects faults itself, never
resubmits and never cancels foreign work. The bearer token comes from
RA_TRIAL_TOKEN; output contains no token or API address.

  plan    validate the frozen plan and print its digest
  submit  create the cohort (and replay keys) -> evidence/submit.json
  mark    append a timestamped operator fault/restart mark
  observe poll lifecycle until settled or the window ends -> evidence/observe.json
  audit   independent count/usage recalculation -> evidence/audit.json + manifest
"""

import argparse
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

PREFIX = "/api/v1/compute"
TERMINAL = {"SUCCEEDED", "FAILED", "RESULT_INVALID", "CANCELED"}
UNRESOLVED = {"SUBMISSION_UNKNOWN"}
LIMITS = {"jobs": 30, "job_seconds": 120, "window_seconds": 1800, "slot_seconds": 1800}


def stamp():
    return datetime.now(UTC).isoformat()


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def load_plan(path):
    plan = json.loads(Path(path).read_text())
    required = {
        "trial_ref",
        "project",
        "workload_ref",
        "candidate_ref",
        "keys",
        "job_seconds",
        "window_seconds",
        "accelerators_per_job",
    }
    if set(plan) - required - {"previous_native_jobs"} or not required <= set(plan):
        raise ValueError("plan fields must be exactly the documented set")
    keys = plan["keys"]
    if len(keys) != len(set(keys)) or not all(k.startswith(plan["trial_ref"] + "-") for k in keys):
        raise ValueError("keys must be unique and prefixed by trial_ref")
    if len(keys) + plan.get("previous_native_jobs", 0) > LIMITS["jobs"]:
        raise ValueError("trial exceeds the 30 native job budget")
    if not 0 < plan["job_seconds"] <= LIMITS["job_seconds"]:
        raise ValueError("job_seconds must be within 120")
    if not 0 < plan["window_seconds"] <= LIMITS["window_seconds"]:
        raise ValueError("window_seconds must be within 1800")
    slots = len(keys) * plan["job_seconds"] * plan["accelerators_per_job"]
    if slots > LIMITS["slot_seconds"]:
        raise ValueError("planned accelerator-slot seconds exceed 1800")
    return plan, slots


class Api:
    def __init__(self, url, token, ca=None):
        self.url, self.token = url.rstrip("/"), token
        self.context = ssl.create_default_context(cafile=ca) if url.startswith("https") else None

    def call(self, method, path, body=None, headers=None):
        request = urllib.request.Request(
            self.url + PREFIX + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15, context=self.context) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, {"detail": "HTTP error"}


def write(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


def submit(api, plan, out):
    records = []
    for key in plan["keys"]:
        request = {"workload_ref": plan["workload_ref"], "candidate_ref": plan["candidate_ref"]}
        first = api.call("POST", "/jobs", request, {"Idempotency-Key": key})
        replay = api.call("POST", "/jobs", request, {"Idempotency-Key": key})
        records.append(
            {
                "key": key,
                "at": stamp(),
                "status": [first[0], replay[0]],
                "job_id": first[1].get("job_id"),
                "attempt_id": first[1].get("attempt_id"),
                "replay_same_identity": (first[1].get("job_id"), first[1].get("attempt_id"))
                == (replay[1].get("job_id"), replay[1].get("attempt_id")),
            }
        )
    return write(out, "submit.json", records)


def observe(api, plan, out):
    jobs = json.loads((out / "submit.json").read_text())
    deadline = time.monotonic() + plan["window_seconds"]
    samples, views = [], {}
    while True:
        for job in jobs:
            if job["job_id"]:
                status, view = api.call("GET", f"/jobs/{job['job_id']}/lifecycle")
                views[job["job_id"]] = view if status == 200 else {"state": "OBSERVATION_ERROR"}
        samples.append({"at": stamp(), "states": {k: v.get("state") for k, v in views.items()}})
        settled = all(v.get("state") in TERMINAL | UNRESOLVED for v in views.values())
        if settled or time.monotonic() >= deadline:
            break
        time.sleep(5)
    return write(out, "observe.json", {"settled": settled, "samples": samples, "final": views})


def recalculate(row):
    """Reservation seconds from raw evidence; None whenever any input is unknown."""
    body = row["body"]
    if body.get("never_submitted"):
        return 0.0
    count = (body.get("observed_allocation") or {}).get("accelerator_count")
    try:
        start = datetime.fromisoformat(body["started_at"])
        end = datetime.fromisoformat(body["backend_finished_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if count is None or start.tzinfo is None or end.tzinfo is None or end < start:
        return None
    return (end - start).total_seconds() * count


def audit(jobs, views, ledger):
    attempts = {j["attempt_id"]: j for j in jobs if j["attempt_id"]}
    rows = [r for r in ledger if r["attempt_id"] in attempts]
    per_attempt = {}
    for row in rows:
        per_attempt[row["attempt_id"]] = per_attempt.get(row["attempt_id"], 0) + 1
    natives = [
        v.get("native_identity", {}).get("external_id") for v in views.values() if v.get("state")
    ]
    natives = [n for n in natives if n]
    findings = []
    for attempt, job in attempts.items():
        state = views.get(job["job_id"], {}).get("state")
        count = per_attempt.get(attempt, 0)
        if state in TERMINAL and count != 1:
            findings.append({"attempt_id": attempt, "issue": "TERMINAL_WITHOUT_ONE_LEDGER_ROW"})
        if state not in TERMINAL and count:
            findings.append({"attempt_id": attempt, "issue": "LEDGER_ROW_BEFORE_TERMINAL"})
    for job in jobs:
        if not job["replay_same_identity"]:
            findings.append({"attempt_id": job["attempt_id"], "issue": "REPLAY_CHANGED_IDENTITY"})
    if len(natives) != len(set(natives)):
        findings.append({"issue": "NATIVE_ID_SHARED_BY_ATTEMPTS"})
    usage = []
    for row in rows:
        independent = recalculate(row)
        recorded = row["allocated_device_seconds"]
        agrees = (
            independent is None
            and recorded is None
            or independent is not None
            and recorded is not None
            and abs(independent - recorded) < 1e-6
        )
        if not agrees:
            findings.append(
                {"attempt_id": row["attempt_id"], "issue": "USAGE_RECALCULATION_DIFFERS"}
            )
        usage.append(
            {
                "attempt_id": row["attempt_id"],
                "outcome": row["body"].get("outcome"),
                "recorded_device_seconds": recorded,
                "independent_device_seconds": independent,
                "uncertainty": row["body"].get("uncertainty"),
            }
        )
    states = {}
    for job in jobs:
        state = views.get(job["job_id"], {}).get("state", "UNOBSERVED")
        states[state] = states.get(state, 0) + 1
    return {
        "attempts": len(attempts),
        "states": states,
        "native_ids": len(set(natives)),
        "ledger_rows": len(rows),
        "known_device_seconds": sum(u["recorded_device_seconds"] or 0 for u in usage),
        "unknown_usage_attempts": sum(u["recorded_device_seconds"] is None for u in usage),
        "usage": usage,
        "findings": findings,
        "passed": not findings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "submit", "mark", "observe", "audit"])
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--api", default=os.environ.get("RA_TRIAL_API"))
    parser.add_argument("--ca", default=None)
    parser.add_argument("--label", default=None)
    args = parser.parse_args()
    plan, slots = load_plan(args.plan)
    if args.action == "plan":
        print(json.dumps({"plan_digest": digest(args.plan.read_bytes()), "slot_seconds": slots}))
        return 0
    if args.action == "mark":
        if not args.label:
            parser.error("--label is required")
        marks = args.evidence / "marks.jsonl"
        args.evidence.mkdir(parents=True, exist_ok=True)
        with marks.open("a") as stream:
            stream.write(json.dumps({"at": stamp(), "label": args.label}) + "\n")
        return 0
    token = os.environ.get("RA_TRIAL_TOKEN")
    if not token or not args.api:
        parser.error("RA_TRIAL_TOKEN and --api/RA_TRIAL_API are required")
    api = Api(args.api, token, args.ca)
    if args.action == "submit":
        submit(api, plan, args.evidence)
    elif args.action == "observe":
        observe(api, plan, args.evidence)
    else:
        jobs = json.loads((args.evidence / "submit.json").read_text())
        views = json.loads((args.evidence / "observe.json").read_text())["final"]
        status, ledger = api.call("GET", "/usage")
        if status != 200:
            print(json.dumps({"error": "usage ledger unavailable"}))
            return 1
        write(args.evidence, "ledger.json", [r for r in ledger if r["project"] == plan["project"]])
        result = audit(jobs, views, ledger)
        write(args.evidence, "audit.json", result)
        files = sorted(p for p in args.evidence.iterdir() if p.name != "manifest.json")
        write(
            args.evidence,
            "manifest.json",
            {
                "plan_digest": digest(args.plan.read_bytes()),
                "files": {p.name: digest(p.read_bytes()) for p in files},
            },
        )
        print(json.dumps({k: result[k] for k in ("attempts", "states", "passed")}))
        return 0 if result["passed"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
