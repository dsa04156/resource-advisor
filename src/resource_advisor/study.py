"""Durable ask → reserved probe → backend → observation → confirmation loop."""

import copy
import random
import statistics
import time
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import insert, select

from .contracts import (
    TERMINAL,
    JobRequest,
    ReplicationPolicy,
    StudyRequest,
    WorkloadSpec,
    now,
    signature,
)
from .policy import compatibility
from .replication import ask_replication
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
        # Preserve idempotency digests for studies created before replication options.
        request_body = request.model_dump(mode="json")
        if request.replication is None:
            request_body.pop("replication")
        if request.fidelity_space_ref is None:
            request_body.pop("fidelity_space_ref")
        if request.fidelity_qualification_ref is None:
            request_body.pop("fidelity_qualification_ref")
        for field in ("transfer_space_ref", "transfer_evidence_ref"):
            if request_body[field] is None:
                request_body.pop(field)
        request_digest = signature(request_body)
        if (
            request.strategy in {"rgpe", "history_warm_start"} and not request.transfer_evidence_ref
        ) or (request.strategy == "mfkg" and not request.fidelity_qualification_ref):
            raise Rejected(
                {
                    "mfkg": "MF_KG_DISABLED: no qualified paired-fidelity group",
                    "rgpe": "RGPE_DISABLED: no validated independent source models",
                    "history_warm_start": "WARM_START_DISABLED: no validated independent sources",
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
                if row["request_digest"] != request_digest:
                    raise Conflict("study idempotency key reused with different request")
                return self.public(row)
            spec = WorkloadSpec.model_validate(
                required(self.store, conn, "workload", request.workload_ref, project)
            )
            policy = spec.profiling
            if not policy.consent:
                raise Rejected("explicit profiling consent is required")
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
            grid_schedule = None
            if request.strategy == "grid_characterization":
                if policy.max_probes < 2 * len(eligible):
                    raise Rejected("grid characterization requires two probes per configuration")
                if (
                    policy.final_validation_seconds
                    < len(eligible) * spec.quality.minimum_repeats * 3
                ):
                    raise Rejected("grid confirmation must cover every configuration")
                grid_schedule, rng = [], random.Random(request.seed)
                for block in range(2):
                    order = sorted(eligible)
                    rng.shuffle(order)
                    grid_schedule.extend({"candidate_ref": c, "block": block} for c in order)
            transfer_space = transfer_evidence = None
            if request.strategy in {"rgpe", "history_warm_start"}:
                from .transfer import TransferSpaces

                transfer_space, transfer_evidence = TransferSpaces(self.service).checked(
                    conn, project, request.transfer_space_ref, request.transfer_evidence_ref
                )
                if transfer_space["request"]["target_workload_ref"] != spec.ref:
                    raise Rejected("transfer target differs from study workload")
                if set(eligible) != {o["candidate_ref"] for o in transfer_space["options"]}:
                    raise Rejected("all transfer candidates must fit the eligible candidate budget")
                initial = len(eligible) * (2 if request.strategy == "rgpe" else 1)
                if policy.max_probes <= initial:
                    raise Rejected("probe budget must cover target checks and a subsequent BO step")
            if request.replication and policy.max_probes < (
                len(eligible) * request.replication.minimum_runs
            ):
                raise Rejected("probe budget must cover the replication initial design")
            fidelity = None
            fidelity_schedule = None
            qualification = seed_evidence = qualification_plan = None
            blocks = 3
            if request.strategy in {"fidelity_calibration", "mfkg"}:
                from .fidelity_space import FidelitySpaces

                if request.fidelity_space_ref is None:
                    raise Rejected("qualified MF-KG requires its explicit fidelity space")
                fidelity = required(
                    self.store, conn, "fidelity_space", request.fidelity_space_ref, project
                )
                FidelitySpaces(self.service)._check_bindings(conn, project, fidelity, current=True)
                if fidelity["request"]["target_workload_ref"] != spec.ref:
                    raise Rejected("fidelity space target differs from study workload")
                if set(eligible) != {o["candidate_ref"] for o in fidelity["options"]}:
                    raise Rejected(
                        "all calibration configurations must fit the eligible candidate budget"
                    )
                if request.fidelity_qualification_ref:
                    from .fidelity_qualification import FidelityQualifications

                    qualification_plan = required(
                        self.store,
                        conn,
                        "fidelity_qualification",
                        request.fidelity_qualification_ref,
                        project,
                    )
                    if qualification_plan["space_digest"] != signature(fidelity):
                        raise Rejected("qualification plan differs from study space")
                    if request.strategy == "mfkg":
                        qualification, seed_evidence = FidelityQualifications(self.service).checked(
                            conn, project, request.fidelity_qualification_ref, fidelity
                        )
                        if len(seed_evidence["observations"]) + policy.max_probes > 192:
                            raise Rejected("MF-KG total observations exceed bounded model capacity")
                    else:
                        blocks = qualification_plan["request"]["blocks"]
                        if self.store.get(
                            conn, "fidelity_qualification_study", request.fidelity_qualification_ref
                        ):
                            raise Rejected(
                                "qualification plan already bound to a single calibration"
                            )
                if request.strategy == "fidelity_calibration" and policy.max_probes < blocks * len(
                    fidelity["options"]
                ):
                    raise Rejected("calibration requires independent blocks for every option")
                if (
                    policy.final_validation_seconds
                    < len(eligible) * spec.quality.minimum_repeats * 3
                ):
                    raise Rejected("final budget must cover every calibration configuration")
                if request.strategy == "fidelity_calibration":
                    rng, fidelity_schedule = random.Random(request.seed), []
                    for block in range(blocks):
                        order = sorted(o["ref"] for o in fidelity["options"])
                        rng.shuffle(order)
                        fidelity_schedule.extend(
                            {"option_ref": option, "block": block} for option in order
                        )
            ref = "study-" + uuid4().hex
            body = {
                "request": request_body,
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
            if fidelity is not None:
                body.update(
                    fidelity_space=fidelity,
                    fidelity_space_digest=signature(fidelity),
                    fidelity_schedule=fidelity_schedule,
                )
            if grid_schedule is not None:
                body["grid_schedule"] = grid_schedule
            if transfer_space is not None:
                body.update(
                    transfer_space=transfer_space,
                    transfer_space_digest=signature(transfer_space),
                    transfer_evidence=transfer_evidence,
                    transfer_evidence_digest=signature(transfer_evidence),
                    historical_source_wall_seconds=transfer_evidence[
                        "historical_source_wall_seconds"
                    ],
                    historical_source_cost_recharged=False,
                )
            if qualification is not None:
                body.update(
                    mf_qualification=qualification,
                    mf_seed_evidence=seed_evidence,
                    mf_calibration_policy=qualification_plan["request"],
                    historical_calibration_cost=qualification["calibration_cost"],
                    historical_calibration_recharged=False,
                )
            elif qualification_plan is not None:
                self.store.put(
                    conn,
                    "fidelity_qualification_study",
                    request.fidelity_qualification_ref,
                    project,
                    {"study_ref": ref},
                )
            conn.execute(
                insert(studies).values(
                    id=ref,
                    project=project,
                    idempotency_key=key,
                    request_digest=request_digest,
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
                        workload_ref=plan.get("workload_ref", body["spec"]["ref"]),
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
        if "transfer_space" in body:
            obs["evaluation_wall_seconds"] = (
                datetime.fromisoformat(jb["finished_at"]) - datetime.fromisoformat(jb["created_at"])
            ).total_seconds()
        if "fidelity_space" in body:
            obs.update(
                workload_ref=plan["workload_ref"],
                workload_digest=plan["workload_digest"],
                option_ref=plan["option_ref"],
                fidelity=plan["fidelity"],
                block=plan.get("block"),
                evaluation_wall_seconds=(
                    datetime.fromisoformat(jb["finished_at"])
                    - datetime.fromisoformat(jb["created_at"])
                ).total_seconds(),
            )
        body["observations"].append(obs)
        if jb.get("thermal_assessment"):
            obs["thermal_assessment"] = jb["thermal_assessment"]
        body["active_plan"] = None
        state = "CONFIRMING" if body["confirmation_schedule"] is not None else "EXPLORING"
        if row["state"] == "CANCEL_REQUESTED":
            state = "CANCELED"
        if plan["mode"] == "confirmation" and not self._feasible(
            obs, WorkloadSpec.model_validate(body["spec"])
        ):
            state, body["stop_reason"] = "ABSTAINED", "INDEPENDENT_CONFIRMATION_FAILED"
        if (
            row["state"] != "CANCEL_REQUESTED"
            and jb.get("thermal_assessment", {}).get("status", "ELIGIBLE_TRACE") != "ELIGIBLE_TRACE"
        ):
            state, body["stop_reason"] = "ABSTAINED", "THERMAL_OBSERVATION_INELIGIBLE"
        if (
            row["state"] != "CANCEL_REQUESTED"
            and body["request"]["strategy"] == "mfkg"
            and self._feasible(obs, WorkloadSpec.model_validate(body["spec"]))
        ):
            values = [
                o["seconds_per_work_unit"]
                for o in body["mf_seed_evidence"]["observations"]
                if o["option_ref"] == obs["option_ref"]
            ]
            measured = obs["measurements"]["elapsed_seconds"] / obs["measurements"]["work_units"]
            tolerance = 1 + body["mf_calibration_policy"]["maximum_relative_bias"]
            if not min(values) / tolerance <= measured <= max(values) * tolerance:
                state, body["stop_reason"] = "ABSTAINED", "MF_OBSERVATION_OUTSIDE_QUALIFIED_RANGE"
            else:
                from .fidelity_qualification import adaptive_ranks_unchanged

                if not adaptive_ranks_unchanged(
                    body["fidelity_space"],
                    body["mf_qualification"],
                    body["mf_seed_evidence"],
                    body["observations"],
                    body["mf_calibration_policy"],
                ):
                    state, body["stop_reason"] = "ABSTAINED", "MF_RANK_RELATION_CHANGED"
        if body["request"]["strategy"] == "mfkg" and row["state"] != "CANCEL_REQUESTED":
            reason = None
            if state == "ABSTAINED" and body.get("stop_reason") in {
                "THERMAL_OBSERVATION_INELIGIBLE",
                "MF_OBSERVATION_OUTSIDE_QUALIFIED_RANGE",
                "INDEPENDENT_CONFIRMATION_FAILED",
                "MF_RANK_RELATION_CHANGED",
            }:
                reason = body["stop_reason"]
            elif obs["outcome"] == "COMPLETED" and not self._feasible(
                obs, WorkloadSpec.model_validate(body["spec"])
            ):
                reason = "MF_OBSERVATION_INFEASIBLE"
            if reason:
                qualification_ref = body["request"]["fidelity_qualification_ref"]
                if not self.store.get(conn, "fidelity_invalidation", qualification_ref):
                    self.store.put(
                        conn,
                        "fidelity_invalidation",
                        qualification_ref,
                        row["project"],
                        {
                            "study_ref": row["id"],
                            "job_id": obs["job_id"],
                            "attempt_id": obs["attempt_id"],
                            "reason": reason,
                            "invalidated_at": now().isoformat(),
                        },
                    )
        self.store.change_study(conn, row, state, body)

    @staticmethod
    def _feasible(obs, spec):
        m = obs.get("measurements")
        return bool(
            obs["outcome"] == "COMPLETED"
            and obs.get("thermal_assessment", {}).get("status", "ELIGIBLE_TRACE")
            == "ELIGIBLE_TRACE"
            and m
            and m["quality_value"] >= spec.quality.minimum
            and m["peak_memory_mib"] <= spec.quality.maximum_peak_memory_mib
        )

    def _transfer_choice(self, project, body, spec, allowed, probes):
        from .rgpe import TransferObservation, ask_rgpe, history_order
        from .transfer import TransferSpaces

        started = time.monotonic()
        request, failure = body["request"], None
        try:
            registry = TransferSpaces(self.service)
            with self.store.transaction() as conn:
                space, evidence = registry.checked(
                    conn, project, request["transfer_space_ref"], request["transfer_evidence_ref"]
                )
            if (
                signature(space) != body["transfer_space_digest"]
                or signature(evidence) != body["transfer_evidence_digest"]
            ):
                raise Rejected("frozen study transfer binding changed")
            if {c.ref for c in allowed} != {o["candidate_ref"] for o in space["options"]}:
                raise Rejected("transfer configurations changed or lost budget")
            if any(not self._feasible(o, spec) or o["evidence_kind"] != "hardware" for o in probes):
                raise Rejected("transfer target check failed or lacks hardware evidence")
            target = [
                TransferObservation(
                    attempt_id=o["attempt_id"],
                    candidate_ref=o["candidate_ref"],
                    elapsed_seconds=o["measurements"]["elapsed_seconds"],
                    evaluation_wall_seconds=o["evaluation_wall_seconds"],
                    peak_memory_mib=o["measurements"]["peak_memory_mib"],
                    quality_value=o["measurements"]["quality_value"],
                    quality_passed=True,
                    memory_passed=True,
                )
                for o in probes
            ]
            problem = registry.problem(space, evidence["sources"], target, request["seed"])
            if request["strategy"] == "history_warm_start":
                order = history_order(problem)
                observed = {o["candidate_ref"] for o in probes}
                unseen = [ref for ref in order["candidate_order"] if ref not in observed]
                if unseen:
                    choice = dict(
                        order, candidate_ref=unseen[0], reason="HISTORY_GUIDED_INITIAL_CANDIDATE"
                    )
                else:
                    choice = ask("qlognei", allowed, probes, spec.quality, request["seed"])
                    choice["warm_start"] = order
                    choice["transfer_phase"] = "TARGET_ONLY_BO_AFTER_WARM_START"
            else:
                choice = ask_rgpe(problem)
        except (Rejected, NotFound) as exc:
            failure = "TRANSFER_EVIDENCE_UNAVAILABLE:" + str(exc)
        except (ArithmeticError, RuntimeError, ValueError, ImportError) as exc:
            failure = "TRANSFER_MODEL_FAILED:" + type(exc).__name__
        if failure:
            choice = ask("qlognei", allowed, probes, spec.quality, request["seed"])
            choice["transfer_fallback"] = failure
            choice["transfer_phase"] = "TARGET_ONLY_BO_FALLBACK"
        choice["transfer_space_digest"] = body["transfer_space_digest"]
        choice["transfer_evidence_digest"] = body["transfer_evidence_digest"]
        choice["source_evidence_ref"] = request["transfer_evidence_ref"]
        choice["planning_seconds"] = time.monotonic() - started
        return choice

    def _plan(self, ref, token):
        with self.store.transaction() as conn:
            row = self.store.study(conn, ref)
            if row["state"] != "PLANNING" or row["body"].get("planning_token") != token:
                raise Conflict("study planning lease changed")
            body = copy.deepcopy(row["body"])
            spec = WorkloadSpec.model_validate(body["spec"])
            candidates = [c for c in spec.candidates if c.ref in body["eligible"]]
            qualified = []
            for c in candidates:
                args = self.service.bundle(conn, row["project"], spec.ref, c.ref)
                if not compatibility(*args):
                    qualified.append(c)
            if "fidelity_space" in body:
                from .fidelity_space import FidelitySpaces

                try:
                    FidelitySpaces(self.service)._check_bindings(
                        conn, row["project"], body["fidelity_space"], current=True
                    )
                    if body["request"]["strategy"] == "mfkg":
                        from .fidelity_qualification import FidelityQualifications

                        FidelityQualifications(self.service).checked(
                            conn,
                            row["project"],
                            body["request"]["fidelity_qualification_ref"],
                            body["fidelity_space"],
                        )
                except Rejected:
                    self.store.change_study(
                        conn,
                        row,
                        "ABSTAINED",
                        dict(body, stop_reason="FIDELITY_EXECUTION_CONTEXT_CHANGED"),
                    )
                    return
        policy = spec.profiling
        remaining = (datetime.fromisoformat(body["deadline_at"]) - now()).total_seconds()
        probes = [o for o in body["observations"] if o["mode"] == "pilot"]
        strategy = body["request"]["strategy"]
        calibrating = strategy == "fidelity_calibration"
        characterizing = strategy == "grid_characterization"
        grid_complete = characterizing and len(probes) == len(body["grid_schedule"])
        if characterizing and any(not self._feasible(o, spec) for o in probes):
            return self._abstain(ref, token, body, "GRID_CHARACTERIZATION_CELL_FAILED")
        mixed = calibrating or strategy == "mfkg"
        calibration_complete = calibrating and len(probes) == len(body["fidelity_schedule"])
        if calibrating and any(not self._feasible(o, spec) for o in probes):
            return self._abstain(ref, token, body, "FIDELITY_CALIBRATION_CELL_FAILED")
        replication_choice = None
        if strategy == "adaptive_replication" and body["confirmation_schedule"] is None:
            if {c.ref for c in qualified} != set(body["eligible"]):
                return self._abstain(ref, token, body, "REPLICATION_EXECUTION_CONTEXT_CHANGED")
            replication_choice = ask_replication(
                qualified,
                probes,
                spec.quality,
                ReplicationPolicy.model_validate(body["request"]["replication"]),
                body["request"]["seed"],
            )
            body["replication_assessment"] = replication_choice
            body["planning_seconds"] += replication_choice["planning_seconds"]
            if spec.baseline_candidate_ref in replication_choice["excluded"]:
                return self._abstain(ref, token, body, "REPLICATION_BASELINE_INFEASIBLE")
        device_probe_budget_exhausted = not any(
            policy.device_seconds[body["units"][c.ref]["unit"]]
            - body["charged_device_seconds"].get(body["units"][c.ref]["unit"], 0)
            > (policy.final_validation_seconds + 1) * body["units"][c.ref]["count"]
            for c in qualified
        )
        mf_choice, mf_stop = None, None
        if strategy == "mfkg":
            if any(not self._feasible(o, spec) for o in probes):
                return self._abstain(ref, token, body, "MF_OBSERVATION_INFEASIBLE")
            if (
                body["confirmation_schedule"] is None
                and len(probes) < policy.max_probes
                and remaining > policy.final_validation_seconds + 3
                and not device_probe_budget_exhausted
            ):
                from .fidelity_space import FidelityEvidenceRequest, FidelitySpaces
                from .mfkg import MFKernelInput, ask_mfkg

                seed = body["mf_seed_evidence"]
                try:
                    evidence = FidelitySpaces(self.service).evidence(
                        row["project"],
                        body["fidelity_space"]["request"]["ref"],
                        FidelityEvidenceRequest(
                            job_ids=tuple(p["job_id"] for p in seed["provenance"])
                            + tuple(o["job_id"] for o in probes),
                            seed=body["request"]["seed"],
                        ),
                    )
                except (Rejected, ValueError):
                    return self._abstain(ref, token, body, "MF_EVIDENCE_NO_LONGER_VALID")
                started = time.monotonic()
                try:
                    mf_choice = ask_mfkg(MFKernelInput.model_validate(evidence["kernel_input"]))
                    mf_stop = None if mf_choice["option_ref"] else "MF_KG_NO_POSITIVE_GAIN"
                except (ArithmeticError, RuntimeError, ValueError, ImportError) as exc:
                    mf_stop = "MF_KG_MODEL_FAILED:" + type(exc).__name__
                planning = time.monotonic() - started
                body["planning_seconds"] += planning
                body["last_mf_analysis"] = {
                    "evidence_ref": evidence["ref"],
                    "analysis": mf_choice,
                    "failure": mf_stop,
                    "planning_seconds": planning,
                }
                if mf_choice:
                    mf_choice["planning_seconds"] = planning
                remaining = (datetime.fromisoformat(body["deadline_at"]) - now()).total_seconds()
        if body["confirmation_schedule"] is None and (
            len(probes) >= policy.max_probes
            or remaining <= policy.final_validation_seconds + 3
            or strategy == "lookup"
            or device_probe_budget_exhausted
            or (replication_choice and replication_choice["stop_exploration"])
            or calibration_complete
            or grid_complete
            or mf_stop
        ):
            if calibrating and not calibration_complete:
                return self._abstain(ref, token, body, "INCOMPLETE_FIDELITY_CALIBRATION_BUDGET")
            if characterizing and not grid_complete:
                return self._abstain(ref, token, body, "INCOMPLETE_GRID_CHARACTERIZATION_BUDGET")
            good = [o for o in probes if self._feasible(o, spec)]
            if mixed:
                # Never compare raw short-run latency with the target workload.
                good = [o for o in good if o["fidelity"] == 1]
            if strategy == "mfkg":
                target_options = {
                    o["ref"]: o["candidate_ref"]
                    for o in body["fidelity_space"]["options"]
                    if o["fidelity"] == 1
                }
                good.extend(
                    {
                        "candidate_ref": target_options[o["option_ref"]],
                        "measurements": {
                            "elapsed_seconds": o["seconds_per_work_unit"] * spec.identity.work_units
                        },
                    }
                    for o in body["mf_seed_evidence"]["observations"]
                    if o["option_ref"] in target_options
                )
                body["exploration_stop_reason"] = mf_stop or "MF_KG_TOTAL_BUDGET_LIMIT"
            if replication_choice:
                good = [o for o in good if o["candidate_ref"] not in replication_choice["excluded"]]
                body["exploration_stop_reason"] = (
                    replication_choice["reason"]
                    if replication_choice["stop_exploration"]
                    else "REPLICATION_TOTAL_BUDGET_LIMIT"
                )
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
            selected = (
                body["eligible"]
                if mixed or characterizing
                else list(dict.fromkeys([spec.baseline_candidate_ref, finalist]))
            )
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
        elif characterizing:
            slot = body["grid_schedule"][len(probes)]
            choice = dict(slot, reason="PREREGISTERED_GRID_CHARACTERIZATION", planning_seconds=0)
            available_wall = (remaining - policy.final_validation_seconds) / (
                len(body["grid_schedule"]) - len(probes)
            )
        elif calibrating:
            slot = body["fidelity_schedule"][len(probes)]
            option = next(
                o for o in body["fidelity_space"]["options"] if o["ref"] == slot["option_ref"]
            )
            choice = {
                "candidate_ref": option["candidate_ref"],
                "option_ref": option["ref"],
                "fidelity": option["fidelity"],
                "block": slot["block"],
                "reason": "PREREGISTERED_FIDELITY_CALIBRATION",
                "planning_seconds": 0,
            }
            available_wall = (remaining - policy.final_validation_seconds) / (
                len(body["fidelity_schedule"]) - len(probes)
            )
        elif strategy == "mfkg":
            choice = {
                **mf_choice,
                "reason": "QUALIFIED_COST_AWARE_MF_KG",
                "qualification_ref": body["request"]["fidelity_qualification_ref"],
                "qualification_digest": signature(body["mf_qualification"]),
                "evidence_ref": body["last_mf_analysis"]["evidence_ref"],
            }
            available_wall = remaining - policy.final_validation_seconds
        else:
            if strategy in {"rgpe", "history_warm_start"}:
                choice = self._transfer_choice(row["project"], body, spec, allowed, probes)
            else:
                choice = replication_choice or ask(
                    strategy, allowed, probes, spec.quality, body["request"]["seed"]
                )
            available_wall = (
                remaining - policy.final_validation_seconds - choice["planning_seconds"]
            )
        if choice is not replication_choice and strategy != "mfkg":
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
        execution_wall = int(available_wall) - collection
        # A run-time upper bound is not a prediction. Giving it every remaining
        # second can leave only one second for real scheduler admission/startup.
        # Reserve up to half the remaining wall budget for queueing first.
        queue_reserve = min(policy.max_queue_seconds, max(1, execution_wall // 2))
        run_limit = min(
            policy.max_wall_seconds_per_candidate,
            int(available_device / unit["count"]),
            execution_wall - queue_reserve,
        )
        queue_limit = min(policy.max_queue_seconds, execution_wall - run_limit)
        if run_limit < 1 or queue_limit < 1:
            return self._abstain(ref, token, body, "RESERVED_CONFIRMATION_BUDGET_PROTECTED")
        plan_ref = "plan-" + uuid4().hex
        execution_spec = spec
        binding = None
        if mixed:
            option = next(
                o
                for o in body["fidelity_space"]["options"]
                if o["candidate_ref"] == candidate.ref
                and (o["fidelity"] == 1 if confirming else o["ref"] == choice["option_ref"])
            )
            binding = next(
                b for b in body["fidelity_space"]["bindings"] if b["option_ref"] == option["ref"]
            )
            with self.store.transaction() as conn:
                execution_spec = WorkloadSpec.model_validate(
                    required(self.store, conn, "workload", binding["workload_ref"], row["project"])
                )
            run_limit = min(
                run_limit,
                execution_spec.profiling.max_wall_seconds_per_candidate,
                execution_spec.execution.max_run_seconds,
            )
            queue_limit = min(
                queue_limit,
                execution_spec.profiling.max_queue_seconds,
                execution_spec.execution.max_queue_seconds,
            )
            collection = min(collection, execution_spec.execution.max_collection_seconds)
        plan = {
            "ref": plan_ref,
            "study_ref": ref,
            "candidate_ref": candidate.ref,
            "workload_digest": signature(execution_spec),
            "workload_ref": execution_spec.ref,
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
        if binding is not None:
            plan.update(
                option_ref=binding["option_ref"],
                fidelity=option["fidelity"],
                block=choice.get("block"),
                fidelity_space_digest=body["fidelity_space_digest"],
            )
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
