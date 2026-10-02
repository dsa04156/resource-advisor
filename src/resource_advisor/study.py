"""Durable ask → reserved probe → backend → observation → confirmation loop."""

import copy
import random
import statistics
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import insert, select

from .contracts import TERMINAL, JobRequest, StudyRequest, WorkloadSpec, now, signature
from .policy import compatibility
from .search import ask, device_unit
from .service import NotFound, Rejected, required
from .store import Conflict, jobs, studies

STUDY_TERMINAL = {"COMPLETED", "ABSTAINED", "CANCELED", "FAILED"}


class Studies:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def create(self, project, request: StudyRequest, key: str):
        if not key or len(key) > 128:
            raise Rejected("study requires a stable 1–128 character Idempotency-Key")
        if request.strategy in {"mfkg", "rgpe"}:
            raise Rejected(
                {
                    "mfkg": "MF_KG_DISABLED: no qualified paired-fidelity group",
                    "rgpe": "RGPE_DISABLED: no validated independent source models",
                }[request.strategy]
            )
        with self.store.transaction() as conn:
            row = (
                conn.execute(
                    select(studies).where(
                        studies.c.project == project, studies.c.idempotency_key == key
                    )
                )
                .mappings()
                .first()
            )
            if row:
                if row["request_digest"] != signature(request):
                    raise Conflict("study idempotency key reused with different request")
                return self.public(row)
            spec = WorkloadSpec.model_validate(
                required(self.store, conn, "workload", request.workload_ref, project)
            )
            policy = spec.profiling
            if not policy.consent:
                raise Rejected("explicit profiling consent is required")
            if spec.identity.task_type == "training":
                raise Rejected("stateful training pilot requires qualified checkpoint isolation")
            if policy.final_validation_seconds < spec.quality.minimum_repeats * 2 * 3:
                raise Rejected(
                    "final budget must cover independent baseline and finalist confirmation"
                )
            if policy.total_wall_seconds <= policy.final_validation_seconds:
                raise Rejected("total budget must leave a probe budget after final reservation")
            eligible, excluded, units = [], {}, {}
            for c in sorted(spec.candidates, key=lambda c: c.ref != spec.baseline_candidate_ref):
                _, _, variant, cap = self.service.bundle(conn, project, spec.ref, c.ref)
                errors = compatibility(spec, c, variant, cap)
                if not variant.pilot_command:
                    errors.append("COOPERATIVE_PILOT_ENTRYPOINT_REQUIRED")
                unit = device_unit(c, variant)
                if unit not in policy.device_seconds:
                    errors.append("EXPLICIT_DEVICE_BUDGET_REQUIRED")
                if errors:
                    excluded[c.ref] = errors
                else:
                    eligible.append(c.ref)
                    units[c.ref] = {
                        "unit": unit,
                        "count": c.context.resources.accelerator_count
                        or c.context.resources.host_cpu,
                    }
            if spec.baseline_candidate_ref not in eligible:
                raise Rejected("baseline is not a qualified consented pilot: " + str(excluded))
            eligible = eligible[: policy.max_candidates]
            ref = "study-" + uuid4().hex
            body = {
                "request": request.model_dump(mode="json"),
                "spec": spec.model_dump(mode="json"),
                "search_space_version": signature(
                    [c.model_dump(mode="json") for c in spec.candidates if c.ref in eligible]
                ),
                "eligible": eligible,
                "excluded": excluded,
                "units": units,
                "created_at": now().isoformat(),
                "deadline_at": (now() + timedelta(seconds=policy.total_wall_seconds)).isoformat(),
                "observations": [],
                "plans": [],
                "active_plan": None,
                "charged_device_seconds": {},
                "planning_seconds": 0.0,
                "confirmation_schedule": None,
                "recommendation_ref": None,
                "final_validation_reserved_seconds": policy.final_validation_seconds,
            }
            conn.execute(
                insert(studies).values(
                    id=ref,
                    project=project,
                    idempotency_key=key,
                    request_digest=signature(request),
                    version=1,
                    state="EXPLORING",
                    body=body,
                )
            )
            return self.public(self.store.study(conn, ref))

    @staticmethod
    def public(row):
        return {
            "ref": row["id"],
            "project_ref": row["project"],
            "state": row["state"],
            "version": row["version"],
            **row["body"],
        }

    def get(self, project, ref):
        with self.store.transaction() as conn:
            row = self.store.study(conn, ref)
            if not row or row["project"] != project:
                raise NotFound("study not found")
            return self.public(row)

    def cancel(self, project, ref):
        with self.store.transaction() as conn:
            row = self.store.study(conn, ref)
            if not row or row["project"] != project:
                raise NotFound("study not found")
            if row["state"] not in STUDY_TERMINAL:
                self.store.change_study(conn, row, "CANCEL_REQUESTED", row["body"])
        return self.get(project, ref)

    def tick_all(self):
        with self.store.transaction() as conn:
            refs = list(
                conn.execute(
                    select(studies.c.id).where(studies.c.state.not_in(STUDY_TERMINAL))
                ).scalars()
            )
        for ref in refs:
            try:
                self.tick(ref)
            except Conflict:
                continue  # Another worker advanced this study.

    def tick(self, ref):
        with self.store.transaction() as conn:
            row = self.store.study(conn, ref)
            if not row or row["state"] in STUDY_TERMINAL:
                return
            body = copy.deepcopy(row["body"])
            active = body["active_plan"]
            project = row["project"]
            expired = now() >= datetime.fromisoformat(body["deadline_at"])
            if active:
                plan = required(self.store, conn, "probe_plan", active, project)
                job = (
                    conn.execute(
                        select(jobs).where(
                            jobs.c.project == project, jobs.c.idempotency_key == "probe-" + active
                        )
                    )
                    .mappings()
                    .first()
                )
                if job and job["state"] in TERMINAL:
                    self._observe(conn, row, body, plan, job)
                    return
            if row["state"] == "CANCEL_REQUESTED" or expired:
                if not active:
                    self.store.change_study(
                        conn,
                        row,
                        "CANCELED" if row["state"] == "CANCEL_REQUESTED" else "ABSTAINED",
                        dict(
                            body,
                            stop_reason="USER_CANCEL" if not expired else "WALL_BUDGET_EXHAUSTED",
                        ),
                    )
                    return
                if job:
                    cancel_job = job["id"]
                else:
                    # No job transaction exists yet; a concurrent submit rechecks the study state.
                    self.store.change_study(
                        conn,
                        row,
                        "CANCELED" if not expired else "ABSTAINED",
                        dict(body, active_plan=None, stop_reason="STOPPED_BEFORE_SUBMISSION"),
                    )
                    return
            else:
                cancel_job = None
            if not active:
                if row["state"] == "PLANNING" and now() < datetime.fromisoformat(
                    body["planning_lease_until"]
                ):
                    return
                token = uuid4().hex
                body["planning_token"] = token
                body["planning_lease_until"] = (now() + timedelta(seconds=120)).isoformat()
                self.store.change_study(conn, row, "PLANNING", body)
        if cancel_job:
            self.service.cancel(project, cancel_job)
        elif active:
            try:
                self.service.submit(
                    project,
                    JobRequest(
                        workload_ref=body["spec"]["ref"],
                        candidate_ref=plan["candidate_ref"],
                        mode=plan["mode"],
                        study_ref=ref,
                        probe_plan_ref=active,
                    ),
                    "probe-" + active,
                )
            except Rejected as exc:
                with self.store.transaction() as conn:
                    current = self.store.study(conn, ref)
                    self.store.change_study(
                        conn,
                        current,
                        "ABSTAINED",
                        dict(
                            current["body"],
                            stop_reason="PROBE_REJECTED:" + str(exc),
                            active_plan=None,
                        ),
                    )
        else:
            self._plan(ref, token)

    def _observe(self, conn, row, body, plan, job):
        jb = job["body"]
        record = self.store.get(conn, "result", jb["attempt_id"])
        result = record["body"] if record and job["state"] != "RESULT_INVALID" else None
        outcome = (
            result["outcome"]
            if result
            else {"OUT_OF_MEMORY": "OOM", "TIMEOUT": "TIMEOUT"}.get(jb.get("error"), "FAILED")
        )
        start, end = jb.get("started_at"), jb.get("backend_finished_at")
        if start and end:
            seconds = max(
                0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
            )
            cost, source = seconds * plan["unit_count"], "scheduler_allocation"
        elif jb.get("cancel_before_submit") or (jb.get("error") or "").startswith("PRE_SUBMISSION"):
            cost, source = 0.0, "confirmed_no_submission"
        else:
            cost, source = (
                plan["reserved_device_seconds"],
                "conservative_reservation_unknown_actual",
            )
        unit = plan["device_unit"]
        body["charged_device_seconds"][unit] = body["charged_device_seconds"].get(unit, 0) + cost
        obs = {
            "plan_ref": plan["ref"],
            "candidate_ref": plan["candidate_ref"],
            "attempt_id": jb["attempt_id"],
            "job_id": job["id"],
            "mode": plan["mode"],
            "outcome": outcome,
            "measurements": result["measurements"] if result else None,
            "evidence_kind": result["evidence_kind"] if result else None,
            "device_seconds": cost,
            "cost_source": source,
            "recorded_at": now().isoformat(),
        }
        body["observations"].append(obs)
        body["active_plan"] = None
        state = "CONFIRMING" if body["confirmation_schedule"] is not None else "EXPLORING"
        if row["state"] == "CANCEL_REQUESTED":
            state = "CANCELED"
        if plan["mode"] == "confirmation" and not self._feasible(
            obs, WorkloadSpec.model_validate(body["spec"])
        ):
            state, body["stop_reason"] = "ABSTAINED", "INDEPENDENT_CONFIRMATION_FAILED"
        self.store.change_study(conn, row, state, body)

    @staticmethod
    def _feasible(obs, spec):
        m = obs.get("measurements")
        return bool(
            obs["outcome"] == "COMPLETED"
            and m
            and m["quality_value"] >= spec.quality.minimum
            and m["peak_memory_mib"] <= spec.quality.maximum_peak_memory_mib
        )

    def _plan(self, ref, token):
        with self.store.transaction() as conn:
            row = self.store.study(conn, ref)
            body = copy.deepcopy(row["body"])
            spec = WorkloadSpec.model_validate(body["spec"])
            candidates = [c for c in spec.candidates if c.ref in body["eligible"]]
            qualified = []
            for c in candidates:
                args = self.service.bundle(conn, row["project"], spec.ref, c.ref)
                if not compatibility(*args):
                    qualified.append(c)
        policy = spec.profiling
        remaining = (datetime.fromisoformat(body["deadline_at"]) - now()).total_seconds()
        probes = [o for o in body["observations"] if o["mode"] == "pilot"]
        strategy = body["request"]["strategy"]
        device_probe_budget_exhausted = not any(
            policy.device_seconds[body["units"][c.ref]["unit"]]
            - body["charged_device_seconds"].get(body["units"][c.ref]["unit"], 0)
            > (policy.final_validation_seconds + 1) * body["units"][c.ref]["count"]
            for c in qualified
        )
        if body["confirmation_schedule"] is None and (
            len(probes) >= policy.max_probes
            or remaining <= policy.final_validation_seconds + 3
            or strategy == "lookup"
            or device_probe_budget_exhausted
        ):
            good = [o for o in probes if self._feasible(o, spec)]
            if strategy == "lookup":
                rec = self.service.recommend(row["project"], spec.ref)
                finalist = rec["candidate_ref"]
            elif good:
                grouped = {
                    c.ref: [
                        o["measurements"]["elapsed_seconds"]
                        for o in good
                        if o["candidate_ref"] == c.ref
                    ]
                    for c in qualified
                }
                finalist = min(
                    (key for key in grouped if grouped[key]),
                    key=lambda key: statistics.mean(grouped[key]),
                    default=None,
                )
            else:
                finalist = None
            if finalist is None:
                return self._abstain(ref, token, body, "NO_MEASURED_FEASIBLE_CONFIG")
            selected = list(dict.fromkeys([spec.baseline_candidate_ref, finalist]))
            schedule = selected * spec.quality.minimum_repeats
            random.Random(body["request"]["seed"]).shuffle(schedule)
            body["confirmation_schedule"] = schedule
            body["predicted_candidate"] = finalist
            body["confirmation_started_at"] = now().isoformat()
        confirmations = [o for o in body["observations"] if o["mode"] == "confirmation"]
        confirming = body["confirmation_schedule"] is not None
        if confirming and len(confirmations) == len(body["confirmation_schedule"]):
            return self._finish(ref, token, body, spec, confirmations)
        allowed = []
        for c in qualified:
            unit = body["units"][c.ref]
            available = policy.device_seconds[unit["unit"]] - body["charged_device_seconds"].get(
                unit["unit"], 0
            )
            reserve = 0 if confirming else policy.final_validation_seconds * unit["count"]
            if available - reserve >= unit["count"]:
                allowed.append(c)
        if confirming:
            target = body["confirmation_schedule"][len(confirmations)]
            choice = {
                "candidate_ref": target,
                "reason": "INDEPENDENT_FINAL_CONFIRMATION",
                "planning_seconds": 0,
            }
            remaining_steps = len(body["confirmation_schedule"]) - len(confirmations)
            final_elapsed = (
                now() - datetime.fromisoformat(body["confirmation_started_at"])
            ).total_seconds()
            available_wall = (
                min(remaining, policy.final_validation_seconds - final_elapsed) / remaining_steps
            )
        else:
            choice = ask(strategy, allowed, probes, spec.quality, body["request"]["seed"])
            available_wall = (
                remaining - policy.final_validation_seconds - choice["planning_seconds"]
            )
        body["planning_seconds"] += choice["planning_seconds"]
        candidate = next((c for c in allowed if c.ref == choice["candidate_ref"]), None)
        if not candidate or available_wall < 3:
            return self._abstain(ref, token, body, "NO_BUDGET_OR_CURRENT_COMPATIBLE_CANDIDATE")
        unit = body["units"][candidate.ref]
        available_device = policy.device_seconds[unit["unit"]] - body["charged_device_seconds"].get(
            unit["unit"], 0
        )
        if not confirming:
            available_device -= policy.final_validation_seconds * unit["count"]
        collection = min(10, max(1, int(available_wall / 10)))
        run_limit = min(
            policy.max_wall_seconds_per_candidate,
            int(available_device / unit["count"]),
            int(available_wall) - collection - 1,
        )
        queue_limit = min(policy.max_queue_seconds, int(available_wall) - run_limit - collection)
        if run_limit < 1 or queue_limit < 1:
            return self._abstain(ref, token, body, "RESERVED_CONFIRMATION_BUDGET_PROTECTED")
        plan_ref = "plan-" + uuid4().hex
        plan = {
            "ref": plan_ref,
            "study_ref": ref,
            "candidate_ref": candidate.ref,
            "workload_digest": signature(spec),
            "mode": "confirmation" if confirming else "pilot",
            "choice": choice,
            "device_unit": unit["unit"],
            "unit_count": unit["count"],
            "reserved_device_seconds": run_limit * unit["count"],
            "execution_limits": {
                "max_run_seconds": run_limit,
                "max_queue_seconds": queue_limit,
                "max_collection_seconds": collection,
            },
            "deadline_at": min(
                datetime.fromisoformat(body["deadline_at"]),
                now() + timedelta(seconds=available_wall),
            ).isoformat(),
            "artifact_prefix": f"studies/{ref}/{plan_ref}/",
            "checkpoint_digest": policy.checkpoint_digest,
            "created_at": now().isoformat(),
        }
        with self.store.transaction() as conn:
            current = self.store.study(conn, ref)
            if current["state"] != "PLANNING" or current["body"].get("planning_token") != token:
                raise Conflict("study planning lease changed")
            self.store.put(conn, "probe_plan", plan_ref, current["project"], plan)
            body["plans"].append(plan_ref)
            body["active_plan"] = plan_ref
            self.store.change_study(
                conn, current, "CONFIRMING" if confirming else "EXPLORING", body
            )

    def _abstain(self, ref, token, body, reason):
        with self.store.transaction() as conn:
            row = self.store.study(conn, ref)
            if row["state"] != "PLANNING" or row["body"].get("planning_token") != token:
                raise Conflict("planning lease changed")
            self.store.change_study(conn, row, "ABSTAINED", dict(body, stop_reason=reason))

    def _finish(self, ref, token, body, spec, confirmations):
        ranking = []
        for candidate in set(body["confirmation_schedule"]):
            runs = [o for o in confirmations if o["candidate_ref"] == candidate]
            if len(runs) < spec.quality.minimum_repeats or not all(
                self._feasible(o, spec) for o in runs
            ):
                return self._abstain(ref, token, body, "CONFIRMATION_INSUFFICIENT")
            values = [o["measurements"]["elapsed_seconds"] for o in runs]
            mean = statistics.mean(values)
            radius = 3 * statistics.stdev(values) / len(values) ** 0.5 if len(values) > 1 else mean
            ranking.append(
                {
                    "candidate_ref": candidate,
                    "mean_seconds": mean,
                    "interval_seconds": [max(0, mean - radius), mean + radius],
                    "evidence_refs": [o["attempt_id"] for o in runs],
                    "independent_runs": len(runs),
                }
            )
        ranking.sort(key=lambda c: c["mean_seconds"])
        baseline = next(c for c in ranking if c["candidate_ref"] == spec.baseline_candidate_ref)
        chosen, status = ranking[0], "CONFIRMED_RECOMMENDATION"
        if chosen != baseline and chosen["interval_seconds"][1] >= baseline["interval_seconds"][0]:
            chosen, status = baseline, "PRESERVE_BASELINE_UNCERTAINTY"
        rec_ref = "rec-" + uuid4().hex
        rec = {
            "ref": rec_ref,
            "workload_ref": spec.ref,
            "workload_digest": signature(spec),
            "status": status,
            "candidate_ref": chosen["candidate_ref"],
            "confirmed_candidate": chosen["candidate_ref"],
            "predicted_candidate": body["predicted_candidate"],
            "study_ref": ref,
            "measured": True,
            "ranking": ranking,
            "approval_required": True,
            "created_at": now().isoformat(),
            "expires_at": (now() + timedelta(minutes=15)).isoformat(),
            "confirmation_run_ids": [o["attempt_id"] for o in confirmations],
            "cost": {
                "wall_seconds": (
                    now() - datetime.fromisoformat(body["created_at"])
                ).total_seconds(),
                "planning_seconds": body["planning_seconds"],
                "device_seconds": body["charged_device_seconds"],
            },
        }
        with self.store.transaction() as conn:
            row = self.store.study(conn, ref)
            if row["state"] != "PLANNING" or row["body"].get("planning_token") != token:
                raise Conflict("planning lease changed")
            self.store.put(conn, "recommendation", rec_ref, row["project"], rec)
            self.store.change_study(
                conn,
                row,
                "COMPLETED",
                dict(
                    body,
                    recommendation_ref=rec_ref,
                    recommendation=rec,
                    recommendation_digest=signature(rec),
                ),
            )
