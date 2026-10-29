"""Frozen, bounded B0–B5 × E1–E8 experiment plan, runner and analysis.

Design: randomized complete blocks. Each block of a scenario/pattern contains
every applicable arm once, in a seeded order, with the same frozen arrival trace;
a cohort settles before the next starts (no carry-over; external state is
captured, not reset). The independent unit is the cohort of native Jobs, not
steps or kernel repetitions. n=3 blocks is a budget-limited exploratory target:
no power, effect size or superiority is claimed. Profiling (≤1 job per route)
precedes evaluation and its profiles are frozen before the first evaluation job.
Historical replay is software validation only and is rejected by the analysis.
"""

import csv
import io
import random
import statistics
import time
from datetime import datetime

from .contracts import now, signature

PLAN_VERSION = "hetero-experiment-plan-v1"
ARMS = {
    "B0": {"name": "native/static", "submission": "fixed_candidate"},
    "B1": {"name": "round robin", "submission": "rotating_candidate"},
    "B2": {"name": "current RA", "submission": "automatic"},
    "B3": {"name": "profile only", "submission": "automatic", "policy": "profile_only"},
    "B4": {"name": "profile+queue", "submission": "automatic", "policy": "profile_queue"},
    "B5": {
        "name": "profile+queue+network",
        "submission": "automatic",
        "policy": "profile_queue_network",
    },
}
# Requirements are capability facts recorded by the operator from read-only
# observation. A missing fact leaves the scenario unsupported; nothing is assumed.
SCENARIOS = {
    "E1": {"title": "single GPU training", "requires": {"gpu_training_routes": 1}},
    "E2": {
        "title": "concurrent training sparse/moderate/burst",
        "requires": {"gpu_training_routes": 2},
        "patterns": ("sparse", "moderate", "burst"),
    },
    "E3": {"title": "same data local/remote", "requires": {"measured_data_links": 2}},
    "E4": {
        "title": "DDP/NCCL",
        "requires": {"exclusive_gpus": 2, "nccl_validated": 1},
        "unsupported_status": "NOT_VALIDATED",
    },
    "E5": {
        "title": "equivalent inference (quality-gated family)",
        "requires": {"equivalent_inference_routes": 2},
    },
    "E6": {
        "title": "Slurm common result with training guard",
        "requires": {"slurm_cpu_training": 1, "kubernetes_cpu_training": 1},
    },
    "E7": {"title": "stale/runtime/queue/network/mismatch reliability", "jobs": 0},
    "E8": {"title": "profiling overhead/ROI", "jobs": 0, "derived_from": ("E1", "E2", "E5", "E6")},
}
ARRIVALS = {"single": (0,), "sparse": (0, 30), "moderate": (0, 10, 20), "burst": (0, 0, 0)}
BUDGET = {
    "max_jobs": 60,
    "max_wall_seconds": 1800,
    "max_slot_seconds": 1800,
    "max_profiling_jobs_per_route": 1,
    "job_timeout_seconds": 120,
    "repeats": 3,
}


def _requirement_gaps(scenario, facts):
    return sorted(
        f"{key}<{need}"
        for key, need in SCENARIOS[scenario].get("requires", {}).items()
        if int(facts.get(key) or 0) < need
    )


def freeze_plan(capacity, *, seed, priority=("E6", "E5", "E1", "E2", "E3", "E4"), budget=None):
    """Prospectively choose a supported subset that fits the budget, then freeze it."""
    budget = {**BUDGET, **(budget or {})}
    facts = capacity["facts"]
    bindings = capacity.get("bindings", {})
    expected = float(capacity["expected_job_seconds"])
    if not 0 < expected <= budget["job_timeout_seconds"]:
        raise ValueError("expected job seconds must lie within the per-job timeout")
    scenarios, units = {}, []
    routes = sorted(
        {(b["workload_ref"], r) for b in bindings.values() for r in b.get("profiling_routes", ())}
    )
    profiling = [
        {
            "unit_id": f"profile-{workload}-{candidate}",
            "phase": "profiling",
            "workload_ref": workload,
            "candidate_ref": candidate,
            "arrivals": [0],
            "purpose": "profile+warmup; excluded from evaluation outcomes",
        }
        for workload, candidate in routes
    ]
    jobs = len(profiling)
    slot_seconds = jobs * expected
    wall = jobs * expected
    for scenario in priority:
        gaps = _requirement_gaps(scenario, facts)
        binding = bindings.get(scenario)
        if gaps or binding is None:
            scenarios[scenario] = {
                "status": SCENARIOS[scenario].get("unsupported_status", "BLOCKED"),
                "reasons": gaps or ["BINDING_MISSING"],
            }
            continue
        arms = [arm for arm in ARMS if arm in binding["profiles"] or arm in {"B0", "B1"}]
        if "B5" in arms and int(facts.get("measured_data_links") or 0) < 1:
            arms.remove("B5")
        patterns = SCENARIOS[scenario].get("patterns", ("single",))
        planned = []
        for pattern in patterns:
            for block in range(1, budget["repeats"] + 1):
                order = random.Random(f"{seed}:{scenario}:{pattern}:{block}").sample(
                    arms, len(arms)
                )
                for position, arm in enumerate(order):
                    planned.append(
                        {
                            "unit_id": f"{scenario}-{pattern}-b{block}-{arm}",
                            "scenario": scenario,
                            "pattern": pattern,
                            "block": block,
                            "position": position,
                            "arm": arm,
                            "arrivals": list(ARRIVALS[pattern]),
                            "workload_ref": binding["workload_ref"],
                        }
                    )
        add_jobs = sum(len(u["arrivals"]) for u in planned)
        add_slot = add_jobs * expected
        add_wall = sum(max(u["arrivals"]) + expected for u in planned)
        if (
            jobs + add_jobs > budget["max_jobs"]
            or slot_seconds + add_slot > budget["max_slot_seconds"]
            or wall + add_wall > budget["max_wall_seconds"]
        ):
            scenarios[scenario] = {"status": "DEFERRED_BUDGET", "reasons": ["BUDGET"]}
            continue
        jobs, slot_seconds, wall = jobs + add_jobs, slot_seconds + add_slot, wall + add_wall
        scenarios[scenario] = {"status": "PLANNED", "arms": arms, "patterns": list(patterns)}
        units.extend(planned)
    for scenario in ("E7", "E8"):
        derived = SCENARIOS[scenario].get("derived_from", ())
        if derived and not any(scenarios.get(s, {}).get("status") == "PLANNED" for s in derived):
            scenarios[scenario] = {"status": "BLOCKED", "reasons": ["NO_SOURCE_EXPERIMENT"]}
        else:
            scenarios[scenario] = {
                "status": "PLANNED",
                "jobs": 0,
                "method": "software fault injection on previews"
                if scenario == "E7"
                else "profiling cost vs observed evaluation differences",
            }
    plan = {
        "version": PLAN_VERSION,
        "frozen_at": now().isoformat(),
        "seed": seed,
        "capacity_observed_at": capacity["observed_at"],
        "capacity": capacity,
        "arms": ARMS,
        "budget": budget,
        "expected_job_seconds": expected,
        "planned": {"jobs": jobs, "slot_seconds": slot_seconds, "wall_seconds": wall},
        "scenarios": dict(sorted(scenarios.items())),
        "profiling": profiling,
        "units": units,
        "analysis": {
            "unit": "cohort of native Jobs per (scenario, pattern, block, arm)",
            "outcomes": [
                "completion rate (failed/timeout/cancelled included in denominator)",
                "completed-only JCT = native submit to native completion",
                "queue wait = native submit to allocation start",
                "censored count (timeout/cancel/budget stop)",
                "fallback and abstention counts",
            ],
            "claims": "exploratory n=3 blocks; no power, effect size or superiority claim",
            "profile_split": "profiles from the profiling phase only, measured before evaluation",
        },
    }
    return plan, signature(plan)


def rerun_commands(plan, plan_path):
    """Unsupported cases need fresh read-only facts and a new frozen plan first."""
    return {
        s: (
            "rtk proxy uv run --locked python examples/hetero_experiment.py freeze "
            "--capacity <fresh-capacity.json> --seed <seed> --out <new-plan.json> && "
            "rtk proxy uv run --locked python examples/hetero_experiment.py run "
            f"--plan <new-plan.json> --digest <new-digest> --only {s} "
            "--profiling-profile <legacy-profile> --execute --api <url>  "
            f"# {s} was {v['status']} in {plan_path}: {','.join(v.get('reasons', []))}"
        )
        for s, v in plan["scenarios"].items()
        if v["status"] != "PLANNED"
    }


def scenario_status(plan):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["scenario", "title", "status", "reasons", "arms"])
    for key, value in plan["scenarios"].items():
        writer.writerow(
            [
                key,
                SCENARIOS[key]["title"],
                value["status"],
                ";".join(value.get("reasons", [])),
                ";".join(value.get("arms", [])),
            ]
        )
    return out.getvalue()


def _epoch(value):
    return datetime.fromisoformat(value).timestamp() if value else None


class Runner:
    """Submits only through the authenticated RA API; cancels only its own jobs."""

    TERMINAL = {"SUCCEEDED", "FAILED", "RESULT_INVALID", "CANCELED"}

    def __init__(
        self,
        plan,
        digest,
        client,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
        write=print,
        capture=None,
    ):
        if signature(plan) != digest:
            raise ValueError("plan digest mismatch: refusing to run an unfrozen plan")
        self.plan, self.digest, self.client = plan, digest, client
        self.clock, self.sleep, self.write = clock, sleep, write
        # Read-only external state recorded before each cohort; never reset.
        self.capture = capture or (lambda: None)
        self.jobs = 0
        self.slot_seconds = 0.0
        self.rr = {}

    def steps(self, only=None):
        return [u for u in self.plan["units"] if only is None or u["scenario"] in only]

    def submit(self, unit, k):
        binding = self.plan["capacity"]["bindings"][unit["scenario"]]
        key = f"{self.digest[7:19]}-{unit['unit_id']}-{k}"
        arm = unit["arm"]
        body = {"workload_ref": unit["workload_ref"]}
        if arm in {"B0", "B1"}:
            if arm == "B0":
                candidate = binding["static_candidate_ref"]
            else:
                order = binding["candidate_order"]
                index = self.rr.get(unit["scenario"], 0)
                self.rr[unit["scenario"]] = index + 1
                candidate = order[index % len(order)]
            preview = self.client.post(
                "/scheduling-plans",
                {
                    "profile_ref": binding["profiles"]["B2"],
                    "workload_ref": unit["workload_ref"],
                    "candidate_ref": candidate,
                },
            )
            if not preview.get("accepted"):
                return {"status": "REJECTED_AT_PREVIEW", "detail": preview.get("excluded")}
            body.update(
                candidate_ref=candidate,
                scheduling_profile_ref=binding["profiles"]["B2"],
                scheduling_plan_digest=preview["digest"],
            )
        else:
            body["scheduling_profile_ref"] = binding["profiles"][arm]
        return self.client.post("/jobs", body, idempotency_key=key)

    def run_profiling(self, *, execute=False, profile_ref):
        """One fixed-candidate job per route under the legacy profile, before evaluation."""
        for unit in self.plan["profiling"]:
            record = {
                "source": "live" if execute else "dry_run",
                "plan_digest": self.digest,
                **unit,
            }
            if not execute:
                self.write(record)
                continue
            if self.jobs + 1 > self.plan["budget"]["max_jobs"]:
                self.write({**record, "status": "NOT_RUN_BUDGET"})
                continue
            preview = self.client.post(
                "/scheduling-plans",
                {
                    "profile_ref": profile_ref,
                    "workload_ref": unit["workload_ref"],
                    "candidate_ref": unit["candidate_ref"],
                },
            )
            if not preview.get("accepted"):
                self.write(
                    {**record, "status": "REJECTED_AT_PREVIEW", "detail": preview.get("excluded")}
                )
                continue
            response = self.client.post(
                "/jobs",
                {
                    "workload_ref": unit["workload_ref"],
                    "candidate_ref": unit["candidate_ref"],
                    "scheduling_profile_ref": profile_ref,
                    "scheduling_plan_digest": preview["digest"],
                },
                idempotency_key=f"{self.digest[7:19]}-{unit['unit_id']}",
            )
            self.jobs += "job_id" in response
            self.settle(record, [response])

    def run(self, *, execute=False, only=None):
        budget = self.plan["budget"]
        started = self.clock()
        for unit in self.steps(only):
            record = {
                "source": "live" if execute else "dry_run",
                "plan_digest": self.digest,
                **unit,
            }
            if not execute:
                self.write(record)
                continue
            expected = len(unit["arrivals"]) * self.plan["expected_job_seconds"]
            if (
                self.jobs + len(unit["arrivals"]) > budget["max_jobs"]
                or self.slot_seconds + expected > budget["max_slot_seconds"]
                or self.clock() - started > budget["max_wall_seconds"]
            ):
                self.write({**record, "status": "NOT_RUN_BUDGET"})
                continue
            record["external_state_before"] = self.capture()
            submitted, base = [], self.clock()
            for k, offset in enumerate(unit["arrivals"]):
                self.sleep(max(0.0, base + offset - self.clock()))
                response = self.submit(unit, k)
                self.jobs += "job_id" in response
                submitted.append(response)
            self.settle(record, submitted)

    def settle(self, record, submitted):
        """Wait for the cohort; cancel own jobs past the per-job timeout (censored)."""
        timeout = self.plan["budget"]["job_timeout_seconds"]
        deadline = self.clock() + timeout + max(record["arrivals"])
        pending = {r["job_id"] for r in submitted if "job_id" in r}
        views = {}
        while pending:
            for job_id in sorted(pending):
                view = self.client.get(f"/jobs/{job_id}/recommendation")
                views[job_id] = view
                if view["state"] in self.TERMINAL:
                    pending.discard(job_id)
            if pending and self.clock() > deadline:
                for job_id in sorted(pending):
                    self.client.post(f"/jobs/{job_id}/cancel", {})
                    views[job_id] = {**views[job_id], "censored": "TIMEOUT_CANCELLED_BY_RUNNER"}
                break
            if pending:
                self.sleep(2.0)
        for view in views.values():
            ts = view.get("native_timestamps", {})
            start, finish = _epoch(ts.get("started_at")), _epoch(ts.get("backend_finished_at"))
            count = max(1, (view.get("allocation") or {}).get("accelerator_count") or 1)
            if start and finish:
                self.slot_seconds += (finish - start) * count
        self.write({**record, "status": "SETTLED", "submissions": submitted, "jobs": views})


def analyze(plan, records):
    """Per (scenario, pattern, arm) outcomes from live records only; CSV text."""
    if any(r.get("source") != "live" for r in records):
        raise ValueError("only live records of this frozen plan are experiment evidence")
    digest = signature(plan)
    if any(r.get("plan_digest") != digest for r in records):
        raise ValueError("records belong to a different plan")
    records = [r for r in records if r.get("phase") != "profiling" and "arm" in r]
    profile_cutoff = min(
        (
            _epoch(job["native_timestamps"]["created_at"])
            for r in records
            for job in r.get("jobs", {}).values()
        ),
        default=None,
    )
    cells = {}
    for record in records:
        key = (record["scenario"], record["pattern"], record["arm"])
        cell = cells.setdefault(
            key,
            {
                "planned": 0,
                "submitted": 0,
                "completed": 0,
                "censored": 0,
                "failed": 0,
                "fallback": 0,
                "abstained": 0,
                "jct": [],
                "wait": [],
                "profile_after_cutoff": 0,
            },
        )
        cell["planned"] += len(record["arrivals"])
        for response in record.get("submissions", []):
            if "job_id" not in response:
                cell["abstained"] += 1
        for job in record.get("jobs", {}).values():
            cell["submitted"] += 1
            rec = job.get("recommendation") or {}
            if rec.get("status") == "LEGACY_FALLBACK":
                cell["fallback"] += 1
            for profile in (rec.get("profiles") or {}).values():
                if profile_cutoff and _epoch(profile["measured_at"]) > profile_cutoff:
                    cell["profile_after_cutoff"] += 1
            ts = job["native_timestamps"]
            if job.get("censored") or job["state"] == "CANCELED":
                cell["censored"] += 1
            elif job["state"] == "SUCCEEDED":
                cell["completed"] += 1
                submit, start = _epoch(ts["scheduler_submitted_at"]), _epoch(ts["started_at"])
                finish = _epoch(ts["backend_finished_at"])
                if submit and finish:
                    cell["jct"].append(finish - submit)
                if submit and start:
                    cell["wait"].append(start - submit)
            else:
                cell["failed"] += 1
    out = io.StringIO()
    fields = [
        "scenario",
        "pattern",
        "arm",
        "planned",
        "submitted",
        "completed",
        "failed",
        "censored",
        "abstained",
        "fallback",
        "completion_rate",
        "jct_completed_mean",
        "jct_completed_median",
        "queue_wait_mean",
        "n_jct",
        "profile_after_cutoff",
    ]
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for (scenario, pattern, arm), c in sorted(cells.items()):
        writer.writerow(
            {
                "scenario": scenario,
                "pattern": pattern,
                "arm": arm,
                **{
                    k: c[k]
                    for k in (
                        "planned",
                        "submitted",
                        "completed",
                        "failed",
                        "censored",
                        "abstained",
                        "fallback",
                        "profile_after_cutoff",
                    )
                },
                "completion_rate": round(c["completed"] / c["planned"], 4) if c["planned"] else "",
                "jct_completed_mean": round(statistics.fmean(c["jct"]), 3) if c["jct"] else "",
                "jct_completed_median": round(statistics.median(c["jct"]), 3) if c["jct"] else "",
                "queue_wait_mean": round(statistics.fmean(c["wait"]), 3) if c["wait"] else "",
                "n_jct": len(c["jct"]),
            }
        )
    return out.getvalue()


def render_reports(plan, results_csv, status_csv):
    """08 tables and 10 summary from analysis outputs only; no superiority wording."""
    digest = signature(plan)
    rows = list(csv.DictReader(io.StringIO(results_csv)))
    statuses = list(csv.DictReader(io.StringIO(status_csv)))

    def table(items, fields):
        lines = ["| " + " | ".join(fields) + " |", "|" + "---|" * len(fields)]
        lines += ["| " + " | ".join(str(i.get(f, "")) for f in fields) + " |" for i in items]
        return "\n".join(lines)

    head = (
        f"Generated from frozen plan `{digest}` live records by "
        "`examples/hetero_experiment.py report`. Exploratory, n≤3 blocks per cell; "
        "no power, effect size or superiority claim.\n"
    )
    results = (
        "# 08. 결과\n\n"
        + head
        + "\n## 실험 상태\n\n"
        + table(statuses, ["scenario", "title", "status", "reasons", "arms"])
        + "\n\n## Cell별 결과 (완료율 분모 = 계획 Job, censored 포함)\n\n"
        + (table(rows, list(rows[0])) if rows else "실행된 live cell 없음.")
        + "\n"
    )
    planned = [s for s in statuses if s["status"] == "PLANNED"]
    summary = (
        "# 10. 요약\n\n"
        + head
        + f"\n- 계획된 실험: {', '.join(s['scenario'] for s in planned) or '없음'}\n"
        + "- 미실행/불가: "
        + (
            ", ".join(
                f"{s['scenario']}={s['status']}" for s in statuses if s["status"] != "PLANNED"
            )
            or "없음"
        )
        + f"\n- live cell 수: {len(rows)}\n"
        + "- 해석은 08의 표와 09-LIMITATIONS를 함께 본다.\n"
    )
    return results, summary


def run_comparison(client, request, *, execute=False, settle=None, timeout_seconds=120):
    """Freeze the four-arm comparison, then (optionally) run one job per arm in turn.

    Each arm is submitted by the server under its frozen plan digest and frozen
    prediction; drift is rejected and recorded as an arm event, never reselected.
    Each job settles before the next arm so no arm's job is queued demand in
    another arm's estimate.
    """
    frozen = client.post("/recommendation-comparisons", request)
    if not execute or "arms" not in frozen:
        return frozen
    settle = settle or (lambda job_id: _settle_one(client, job_id, timeout_seconds))
    for arm in frozen["arm_order"]:
        if not frozen["arms"][arm]["accepted"] or frozen["executed"][arm]:
            continue
        response = client.post(f"/recommendation-comparisons/{frozen['ref']}/arms/{arm}/jobs", {})
        if "job_id" in response:
            settle(response["job_id"])
    return client.get(f"/recommendation-comparisons/{frozen['ref']}")


def _settle_one(client, job_id, timeout_seconds, *, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout_seconds
    while client.get(f"/jobs/{job_id}/recommendation")["state"] not in Runner.TERMINAL:
        if clock() > deadline:
            client.post(f"/jobs/{job_id}/cancel", {})  # own job only; censored
            return
        sleep(2.0)
