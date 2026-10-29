#!/usr/bin/env python3
"""Bounded integrated-operations trial against an existing Compute API.

Standard library only. It submits a frozen cohort with fixed idempotency keys,
replays each key once, records operator fault marks, observes lifecycle until
every attempt is terminal or explicitly unresolved, then independently
recalculates usage from raw ledger rows. It never injects faults itself, never
resubmits and never cancels foreign work. The bearer token comes from
RA_TRIAL_TOKEN; output contains no token or API address.

The whole-effort budget lives in one ledger file (--budget) shared by every
cohort: native jobs, accelerator-slot seconds from each job's actual execution
limit and requested accelerators, and one live window that starts with the
first submission. A cohort is closed only by an audit showing every attempt
natively terminal or explicitly unresolved with no active native execution;
the next cohort is refused until then.

  plan    validate the frozen plan and print its digest
  submit  create the cohort (and replay keys) -> evidence/submit.json
  mark    append a timestamped operator fault/restart mark
  observe poll lifecycle until settled or the window ends -> evidence/observe.json
  audit   independent count/usage recalculation against operator-collected native
          receipts (--native) -> evidence/audit.json + manifest
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
    if set(plan) != required:
        raise ValueError("plan fields must be exactly the documented set")
    keys = plan["keys"]
    if len(keys) != len(set(keys)) or not all(k.startswith(plan["trial_ref"] + "-") for k in keys):
        raise ValueError("keys must be unique and prefixed by trial_ref")
    if len(keys) > LIMITS["jobs"]:
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


def load_budget(path):
    if not path.exists():
        return {"limits": LIMITS, "window_started_at": None, "cohorts": []}
    return json.loads(path.read_text())


def save_budget(path, budget):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(budget, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def window_end(budget):
    started = datetime.fromisoformat(budget["window_started_at"])
    return started.timestamp() + LIMITS["window_seconds"]


def used(budget):
    return (
        sum(c["native_jobs"] for c in budget["cohorts"]),
        sum(c["slot_seconds"] for c in budget["cohorts"]),
    )


def admit(budget, plan, slots):
    """Refuse a cohort the whole-effort budget cannot cover; reserve it otherwise."""
    if any(not c["closed"] for c in budget["cohorts"]):
        raise ValueError("previous cohort is not closed by a contained audit")
    if plan["trial_ref"] in {c["trial_ref"] for c in budget["cohorts"]}:
        raise ValueError("trial_ref was already submitted")
    jobs, slot_seconds = used(budget)
    if jobs + len(plan["keys"]) > LIMITS["jobs"]:
        raise ValueError("cohort exceeds the remaining native job budget")
    if slot_seconds + slots > LIMITS["slot_seconds"]:
        raise ValueError("cohort exceeds the remaining accelerator-slot budget")
    if budget["window_started_at"] is None:
        budget["window_started_at"] = stamp()
    if time.time() >= window_end(budget):
        raise ValueError("the live workload window has ended")
    cohort = {
        "trial_ref": plan["trial_ref"],
        "submitted_at": stamp(),
        "native_jobs": len(plan["keys"]),
        "slot_seconds": slots,
        "closed": False,
    }
    budget["cohorts"].append(cohort)
    return cohort


def actual_slots(view):
    """Slot seconds bounded by the job's own execution limit, not the plan's claim."""
    limits = view.get("execution_limits") or {}
    resources = view.get("requested_resources") or {}
    seconds, count = limits.get("max_run_seconds"), resources.get("accelerator_count")
    if not isinstance(seconds, int | float) or not isinstance(count, int | float):
        return None, None
    return seconds, seconds * count


def submit(api, plan, out, budget_path):
    budget = load_budget(budget_path)
    planned = len(plan["keys"]) * plan["job_seconds"] * plan["accelerators_per_job"]
    cohort = admit(budget, plan, planned)
    # Reserve before the first native call: a crash still counts the cohort.
    save_budget(budget_path, budget)
    records, actual, stop = [], 0, None
    for key in plan["keys"]:
        request = {"workload_ref": plan["workload_ref"], "candidate_ref": plan["candidate_ref"]}
        first = api.call("POST", "/jobs", request, {"Idempotency-Key": key})
        replay = api.call("POST", "/jobs", request, {"Idempotency-Key": key})
        record = {
            "key": key,
            "at": stamp(),
            "status": [first[0], replay[0]],
            "job_id": first[1].get("job_id"),
            "attempt_id": first[1].get("attempt_id"),
            "replay_same_identity": (first[1].get("job_id"), first[1].get("attempt_id"))
            == (replay[1].get("job_id"), replay[1].get("attempt_id")),
        }
        records.append(record)
        status, view = (
            api.call("GET", f"/jobs/{record['job_id']}/view") if record["job_id"] else (0, {})
        )
        seconds, slots = actual_slots(view) if status == 200 else (None, None)
        record["max_run_seconds"], record["slot_seconds"] = seconds, slots
        per_job = plan["job_seconds"] * plan["accelerators_per_job"]
        if seconds is None:
            stop, slots = "LIMITS_UNOBSERVED", per_job
        elif seconds > plan["job_seconds"] or slots > per_job:
            stop = "LIMITS_EXCEED_PLAN"
        actual += slots
        if stop:
            break
    # Stopped early: charge what was actually submitted, including the offending job.
    cohort["native_jobs"] = len(records) if stop else len(plan["keys"])
    cohort["slot_seconds"] = actual if stop else planned
    cohort["stopped"] = stop
    save_budget(budget_path, budget)
    write(out, "submit.json", records)
    return stop


def observe(api, plan, out, budget_path):
    jobs = json.loads((out / "submit.json").read_text())
    budget = load_budget(budget_path)
    cohort = next(c for c in budget["cohorts"] if c["trial_ref"] == plan["trial_ref"])
    # One deadline per cohort, never later than the whole-effort window.
    started = datetime.fromisoformat(cohort["submitted_at"]).timestamp()
    deadline = min(started + plan["window_seconds"], window_end(budget))
    samples, views = [], {}
    while True:
        for job in jobs:
            if job["job_id"]:
                status, view = api.call("GET", f"/jobs/{job['job_id']}/lifecycle")
                views[job["job_id"]] = view if status == 200 else {"state": "OBSERVATION_ERROR"}
        samples.append({"at": stamp(), "states": {k: v.get("state") for k, v in views.items()}})
        settled = len(views) == len(jobs) and all(
            v.get("state") in TERMINAL | UNRESOLVED for v in views.values()
        )
        if settled or time.time() >= deadline:
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


def audit(jobs, observed, ledger, native, keys):
    """Independent closure check. `native` maps attempt ID to the operator's raw
    scheduler receipts ({"native_id", "terminal"}); the API is not trusted for
    the execution count. Unresolved attempts never pass and never become zero."""
    findings = []
    views = observed.get("final", {})
    if not observed.get("settled"):
        findings.append({"issue": "OBSERVATION_NOT_SETTLED"})
    if sorted(j["key"] for j in jobs) != sorted(keys):
        findings.append({"issue": "SUBMISSIONS_DO_NOT_MATCH_PLAN"})
    for job in jobs:
        if (
            not job["job_id"]
            or not job["attempt_id"]
            or not all(200 <= code < 300 for code in job["status"])
        ):
            findings.append({"key": job["key"], "issue": "SUBMISSION_WITHOUT_IDENTITY"})
        elif not job["replay_same_identity"]:
            findings.append({"attempt_id": job["attempt_id"], "issue": "REPLAY_CHANGED_IDENTITY"})
    attempts = {j["attempt_id"]: j for j in jobs if j["job_id"] and j["attempt_id"]}
    rows = [r for r in ledger if r["attempt_id"] in attempts]
    per_attempt = {}
    for row in rows:
        per_attempt[row["attempt_id"]] = per_attempt.get(row["attempt_id"], 0) + 1
    contained, unresolved, natives = True, 0, []
    for attempt, job in attempts.items():
        view = views.get(job["job_id"]) or {}
        state = view.get("state")
        receipts = native.get(attempt)
        if receipts is None:
            findings.append({"attempt_id": attempt, "issue": "NATIVE_RECEIPT_MISSING"})
            contained = False
            continue
        natives += [r["native_id"] for r in receipts]
        active = [r for r in receipts if not r.get("terminal")]
        if active:
            contained = False
            findings.append({"attempt_id": attempt, "issue": "NATIVE_EXECUTION_ACTIVE"})
        count = per_attempt.get(attempt, 0)
        external = (view.get("native_identity") or {}).get("external_id")
        zero = bool(view.get("native_rejection")) or bool(
            (view.get("usage") or {}).get("never_submitted")
        )
        if state in TERMINAL:
            if count != 1:
                findings.append({"attempt_id": attempt, "issue": "TERMINAL_WITHOUT_ONE_LEDGER_ROW"})
            expected = [] if zero else [external]
            if [r["native_id"] for r in receipts] != expected:
                findings.append({"attempt_id": attempt, "issue": "NATIVE_COUNT_DISAGREES"})
        elif state in UNRESOLVED:
            unresolved += 1
            findings.append({"attempt_id": attempt, "issue": "UNRESOLVED_SUBMISSION"})
            if count:
                findings.append({"attempt_id": attempt, "issue": "LEDGER_ROW_BEFORE_TERMINAL"})
            if receipts:
                findings.append({"attempt_id": attempt, "issue": "NATIVE_JOB_NOT_ADOPTED"})
        else:
            contained = False
            findings.append({"attempt_id": attempt, "issue": "NOT_TERMINAL", "state": state})
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
        state = (views.get(job["job_id"]) or {}).get("state", "UNOBSERVED")
        states[state] = states.get(state, 0) + 1
    return {
        "attempts": len(attempts),
        "states": states,
        "native_ids": len(set(natives)),
        "ledger_rows": len(rows),
        "known_device_seconds": sum(u["recorded_device_seconds"] or 0 for u in usage),
        "unknown_usage_attempts": sum(u["recorded_device_seconds"] is None for u in usage),
        "unresolved_attempts": unresolved,
        "usage": usage,
        "findings": findings,
        # Contained: no active native execution; unresolved stays visible as a finding.
        "contained": contained and observed.get("settled", False),
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
    parser.add_argument("--budget", type=Path, default=None, help="whole-effort ledger")
    parser.add_argument("--native", type=Path, default=None, help="native receipts JSON")
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
    if not token or not args.api or not args.budget:
        parser.error("RA_TRIAL_TOKEN, --api/RA_TRIAL_API and --budget are required")
    api = Api(args.api, token, args.ca)
    if args.action == "submit":
        stop = submit(api, plan, args.evidence, args.budget)
        print(json.dumps({"stopped": stop}))
        return 1 if stop else 0
    elif args.action == "observe":
        observe(api, plan, args.evidence, args.budget)
    else:
        if not args.native:
            parser.error("--native receipts are required for audit")
        jobs = json.loads((args.evidence / "submit.json").read_text())
        observed = json.loads((args.evidence / "observe.json").read_text())
        native = json.loads(args.native.read_text())
        write(args.evidence, "native.json", native)
        status, ledger = api.call("GET", "/usage")
        if status != 200:
            print(json.dumps({"error": "usage ledger unavailable"}))
            return 1
        write(args.evidence, "ledger.json", [r for r in ledger if r["project"] == plan["project"]])
        result = audit(jobs, observed, ledger, native, plan["keys"])
        write(args.evidence, "audit.json", result)
        if result["contained"]:
            budget = load_budget(args.budget)
            for cohort in budget["cohorts"]:
                if cohort["trial_ref"] == plan["trial_ref"]:
                    cohort["closed"], cohort["closed_at"] = True, stamp()
            save_budget(args.budget, budget)
        files = sorted(p for p in args.evidence.iterdir() if p.name != "manifest.json")
        write(
            args.evidence,
            "manifest.json",
            {
                "plan_digest": digest(args.plan.read_bytes()),
                "files": {p.name: digest(p.read_bytes()) for p in files},
            },
        )
        print(json.dumps({k: result[k] for k in ("attempts", "states", "contained", "passed")}))
        return 0 if result["passed"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
