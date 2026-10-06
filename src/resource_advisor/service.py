"""Project-scoped contracts, durable submission and evidence-bound recommendations."""

import math
import statistics
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from .contracts import (
    TERMINAL,
    ApprovalRequest,
    CapabilitySnapshot,
    ExecutionResult,
    JobRequest,
    JobTemplate,
    RuntimeVariant,
    State,
    WorkloadSpec,
    now,
    signature,
)
from .policy import compatibility, context_signature, execution_compatibility
from .store import Conflict, jobs


class NotFound(ValueError):
    pass


class Rejected(ValueError):
    pass


def required(store, conn, kind, ref, project):
    item = store.get(conn, kind, ref)
    if not item or item["project"] not in {project, "*"}:
        raise NotFound(f"{kind} not found")
    return item["body"]


class Service:
    def __init__(
        self, store, *, accept_synthetic=False, operational_mode=False, console_workloads=()
    ):
        self.store = store
        self.accept_synthetic = accept_synthetic
        self.operational_mode = operational_mode
        self.console_workloads = tuple(console_workloads)

    def register(self, kind, model, project):
        if hasattr(model, "project_ref") and model.project_ref != project:
            raise Rejected("project does not match authenticated principal")
        with self.store.transaction() as conn:
            return self.store.put(conn, kind, model.ref, project, model.model_dump(mode="json"))[
                "body"
            ]

    @staticmethod
    def template_spec(spec, template):
        if (
            template.max_run_seconds > spec.execution.max_run_seconds
            or template.max_queue_seconds > spec.execution.max_queue_seconds
        ):
            raise Rejected("템플릿 시간 제한은 원본 실행 환경의 한도를 넘을 수 없습니다.")
        body = spec.model_dump(mode="json")
        body["execution"].update(
            priority=template.priority,
            max_run_seconds=template.max_run_seconds,
            max_queue_seconds=template.max_queue_seconds,
        )
        return WorkloadSpec.model_validate(body)

    def register_template(self, project, template):
        with self.store.transaction() as conn:
            spec, candidate, variant, cap = self.bundle(
                conn, project, template.workload_ref, template.candidate_ref
            )
            self.template_spec(spec, template)
            # Reference existing execution bindings; never synthesize verification or a new command.
            return self.store.put(
                conn, "job_template", template.ref, project, template.model_dump(mode="json")
            )["body"]

    def bundle(self, conn, project, workload_ref, candidate_ref):
        spec = WorkloadSpec.model_validate(
            required(self.store, conn, "workload", workload_ref, project)
        )
        candidate = next((c for c in spec.candidates if c.ref == candidate_ref), None)
        if not candidate:
            raise NotFound("candidate not found")
        variant = RuntimeVariant.model_validate(
            required(self.store, conn, "variant", candidate.variant_ref, project)
        )
        cap = CapabilitySnapshot.model_validate(
            required(self.store, conn, "capability", candidate.capability_ref, project)
        )
        if spec.identity.task_type == "training":
            from .training import validate_binding

            binding = required(self.store, conn, "training_isolation", spec.ref, project)
            try:
                validate_binding(binding, spec, candidate, variant)
            except ValueError as exc:
                raise Rejected("qualified training isolation required") from exc
        if spec.identity.sampling_policy_digest is not None:
            from .sampling import SamplingPolicies

            if not self.store.get(conn, "sampling_binding", spec.ref):
                raise Rejected("sampling binding required before workload execution")
            SamplingPolicies(self).checked(conn, project, spec.ref)
        if variant.thermal_policy and (
            spec.identity.task_type not in {"inference", "benchmark"}
            or spec.identity.work_units > 32
            or candidate.context.allocation_mode != "physical_device"
            or candidate.context.resources.accelerator_count != 1
        ):
            raise Rejected(
                "thermal brackets require 1–32 inference/benchmark units on one physical GPU"
            )
        return spec, candidate, variant, cap

    def submit(self, project: str, request: JobRequest, key: str):
        request_body = request.model_dump(mode="json")
        # Optional ownership must not invalidate pre-upgrade idempotency keys.
        if request.template_ref is None:
            request_body.pop("template_ref")
        if request.owner_lease_seconds is None:
            request_body.pop("owner_lease_seconds")
        request_digest = signature(request_body)
        if not key or len(key) > 128:
            raise Rejected("Idempotency-Key must contain 1–128 characters")
        try:
            with self.store.transaction() as conn:
                existing = (
                    conn.execute(
                        select(jobs).where(jobs.c.project == project, jobs.c.idempotency_key == key)
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    if existing["request_digest"] != request_digest:
                        raise Conflict("idempotency key reused with different request")
                    return self.public_job(existing)
                spec, candidate, variant, cap = self.bundle(
                    conn, project, request.workload_ref, request.candidate_ref
                )
                template = None
                if request.template_ref:
                    template = JobTemplate.model_validate(
                        required(self.store, conn, "job_template", request.template_ref, project)
                    )
                    if (
                        request.mode != "observe"
                        or request.approval_ref
                        or request.study_ref
                        or request.probe_plan_ref
                        or template.workload_ref != request.workload_ref
                        or template.candidate_ref != request.candidate_ref
                    ):
                        raise Rejected("템플릿과 작업 요청이 일치하지 않습니다.")
                    spec = self.template_spec(spec, template)
                operational = (
                    self.operational_mode and request.mode == "observe" and not request.approval_ref
                )
                errors = execution_compatibility(
                    spec, candidate, variant, cap, operational=operational
                )
                if errors:
                    raise Rejected(",".join(errors))
                plan = None
                if request.mode in {"pilot", "confirmation"}:
                    if not request.study_ref or not request.probe_plan_ref:
                        raise Rejected(
                            "pilot execution is not enabled without a reserved study plan"
                        )
                    study = self.store.study(conn, request.study_ref)
                    if not study or study["project"] != project:
                        raise NotFound("study not found")
                    plan = required(self.store, conn, "probe_plan", request.probe_plan_ref, project)
                    if (
                        study["state"] not in {"EXPLORING", "CONFIRMING"}
                        or study["body"]["active_plan"] != request.probe_plan_ref
                        or plan["study_ref"] != request.study_ref
                        or plan["mode"] != request.mode
                        or plan["candidate_ref"] != candidate.ref
                        or plan["workload_digest"] != signature(spec)
                        or key != "probe-" + request.probe_plan_ref
                        or now() >= datetime.fromisoformat(plan["deadline_at"])
                    ):
                        raise Rejected(
                            "probe does not match an active, unexpired budget reservation"
                        )
                    if study["body"]["request"]["strategy"] == "mfkg":
                        from .fidelity_qualification import FidelityQualifications

                        FidelityQualifications(self).checked(
                            conn,
                            project,
                            study["body"]["request"]["fidelity_qualification_ref"],
                            study["body"]["fidelity_space"],
                        )
                    self.store.change_study(conn, study, study["state"], study["body"])
                elif request.study_ref or request.probe_plan_ref:
                    raise Rejected("study references require pilot or confirmation mode")
                if request.approval_ref and plan is not None:
                    raise Rejected(
                        "study probes use their reserved plan, not a recommendation approval"
                    )
                if (
                    candidate.ref != spec.baseline_candidate_ref
                    and plan is None
                    and not request.approval_ref
                    and not operational
                ):
                    raise Rejected("non-baseline execution requires an immutable approval")
                # Baseline runs need no approval, but a supplied reference must be
                # genuine: otherwise arbitrary runs can falsely claim recommendation provenance.
                if request.approval_ref:
                    approval = required(self.store, conn, "approval", request.approval_ref, project)
                    if (
                        approval["candidate_ref"] != candidate.ref
                        or approval["workload_digest"] != signature(spec)
                        or datetime.fromisoformat(approval["expires_at"]) < now()
                    ):
                        raise Rejected("approval expired or configuration mismatch")
                    self.check_approval_evidence(conn, project, approval)
                job_id, attempt_id = "j-" + uuid4().hex, "a-" + uuid4().hex
                body = {
                    "request": request_body,
                    "attempt_id": attempt_id,
                    "epoch": 1,
                    "workload_signature": signature(spec.identity),
                    "context_signature": context_signature(candidate, variant),
                    "spec": spec.model_dump(mode="json"),
                    "candidate": candidate.model_dump(mode="json"),
                    "variant": variant.model_dump(mode="json"),
                    "capability": cap.model_dump(mode="json"),
                    "created_at": now().isoformat(),
                    "external_id": None,
                    "operational_submission": operational,
                    "submission_warnings": compatibility(spec, candidate, variant, cap)
                    if operational
                    else [],
                    "backend_cluster_id": cap.backend_cluster_id,
                    "execution_limits": plan["execution_limits"]
                    if plan
                    else spec.execution.model_dump(),
                    "deadline_at": plan["deadline_at"] if plan else None,
                    "artifact_prefix": plan["artifact_prefix"]
                    if plan
                    else f"jobs/{job_id}/{attempt_id}/",
                    "effective_command": list(variant.pilot_command)
                    if request.mode == "pilot"
                    else list(variant.command),
                }
                if template:
                    body["job_template"] = template.model_dump(mode="json")
                if spec.identity.sampling_policy_digest is not None:
                    from .sampling import SamplingPolicies

                    body["sampling_binding"] = SamplingPolicies(self).checked(
                        conn, project, spec.ref
                    )
                    body["sampling_policy"] = required(
                        self.store,
                        conn,
                        "sampling_policy",
                        body["sampling_binding"]["plan"]["policy_ref"],
                        project,
                    )
                if request.owner_lease_seconds is not None:
                    body["owner_lease_expires_at"] = (
                        now() + timedelta(seconds=request.owner_lease_seconds)
                    ).isoformat()
                if spec.identity.task_type == "training":
                    body["training_isolation"] = required(
                        self.store, conn, "training_isolation", spec.ref, project
                    )
                conn.execute(
                    insert(jobs).values(
                        id=job_id,
                        project=project,
                        idempotency_key=key,
                        request_digest=request_digest,
                        state=State.VALIDATED,
                        epoch=1,
                        version=1,
                        body=body,
                    )
                )
                self.store.enqueue(conn, f"submit-{attempt_id}", "submit", {"job_id": job_id})
                return self.public_job(self.store.job(conn, job_id))
        except IntegrityError:
            # A racing identical request committed first; do not issue another submission.
            with self.store.transaction() as conn:
                row = (
                    conn.execute(
                        select(jobs).where(jobs.c.project == project, jobs.c.idempotency_key == key)
                    )
                    .mappings()
                    .first()
                )
                if row and row["request_digest"] == request_digest:
                    return self.public_job(row)
            raise Conflict("concurrent immutable write; retry with the same key") from None

    @staticmethod
    def public_job(row):
        body = row["body"]
        return {
            "job_id": row["id"],
            "project_ref": row["project"],
            "state": row["state"],
            **{
                key: body.get(key)
                for key in [
                    "attempt_id",
                    "epoch",
                    "created_at",
                    "external_id",
                    "workload_signature",
                    "context_signature",
                    "error",
                    "result_digest",
                    "last_observation_error",
                    "last_observation_error_at",
                    "cancel_requested_at",
                    "cancel_dispatch_started_at",
                    "cancel_acknowledged_at",
                    "owner_lease_expires_at",
                    "owner_last_heartbeat_at",
                    "operational_submission",
                    "submission_warnings",
                ]
            },
        }

    def get_job(self, project, job_id):
        with self.store.transaction() as conn:
            row = self.store.job(conn, job_id)
            if not row or row["project"] != project:
                raise NotFound("job not found")
            return self.public_job(row)

    def cancel(self, project, job_id):
        with self.store.transaction() as conn:
            row = self.store.job(conn, job_id)
            if not row or row["project"] != project:
                raise NotFound("job not found")
            if row["state"] not in TERMINAL:
                self.request_cancel(conn, row)
            return self.public_job(self.store.job(conn, job_id))

    def request_cancel(self, conn, row, *, reason=None):
        body = dict(row["body"])
        body.setdefault("cancel_before_submit", row["state"] == State.VALIDATED)
        if reason:
            body["error"] = reason
        self.store.change_job(conn, row, State.CANCEL_REQUESTED, body)
        self.store.enqueue(conn, "cancel-" + body["attempt_id"], "cancel", {"job_id": row["id"]})

    def heartbeat(self, project, job_id):
        with self.store.transaction() as conn:
            row = self.store.job(conn, job_id)
            if not row or row["project"] != project:
                raise NotFound("job not found")
            seconds = row["body"]["request"].get("owner_lease_seconds")
            if seconds is None:
                raise Rejected("job has no workflow ownership lease")
            if row["state"] not in TERMINAL | {State.CANCEL_REQUESTED, State.COLLECTING}:
                stamp = now()
                if stamp >= datetime.fromisoformat(row["body"]["owner_lease_expires_at"]):
                    self.request_cancel(conn, row, reason="OWNER_LEASE_EXPIRED")
                else:
                    body = dict(
                        row["body"],
                        owner_last_heartbeat_at=stamp.isoformat(),
                        owner_lease_expires_at=(stamp + timedelta(seconds=seconds)).isoformat(),
                    )
                    self.store.change_job(conn, row, row["state"], body)
            return self.public_job(self.store.job(conn, job_id))

    def ingest(
        self,
        project,
        result: ExecutionResult,
        artifact_digest: str,
        *,
        phase_profile=None,
        training_receipt=None,
        sampling_receipt=None,
        thermal_trace=None,
        load_trace=None,
    ):
        """Trusted collector only. Require backend completion before result acceptance."""
        with self.store.transaction() as conn:
            row = self.store.job(conn, result.job_id)
            if not row or row["project"] != project:
                raise NotFound("job not found")
            body = dict(row["body"])
            if result.attempt_id != body["attempt_id"] or result.epoch != row["epoch"]:
                raise Conflict("stale or unrelated attempt")
            digest = signature(result)
            if row["state"] in TERMINAL:
                if body.get("result_digest") == digest:
                    load = self.store.get(conn, "load_trace", result.attempt_id)
                    if load_trace is not None and (
                        load is None or signature(load["body"]) != signature(load_trace)
                    ):
                        raise Conflict("terminal load trace is immutable")
                    thermal = self.store.get(conn, "thermal_trace", result.attempt_id)
                    if thermal_trace is not None and (
                        thermal is None or signature(thermal["body"]) != signature(thermal_trace)
                    ):
                        raise Conflict("terminal thermal trace is immutable")
                    sampling = self.store.get(conn, "sampling_receipt", result.attempt_id)
                    if sampling_receipt is not None and (
                        sampling is None
                        or signature(sampling["body"]) != signature(sampling_receipt)
                    ):
                        raise Conflict("terminal sampling receipt is immutable")
                    training = self.store.get(conn, "training_receipt", result.attempt_id)
                    if training_receipt is not None and (
                        training is None
                        or signature(training["body"]) != signature(training_receipt)
                    ):
                        raise Conflict("terminal training receipt is immutable")
                    stored = self.store.get(conn, "phase_profile", result.attempt_id)
                    if phase_profile is not None and (
                        stored is None or signature(stored["body"]) != signature(phase_profile)
                    ):
                        raise Conflict("terminal phase profile is immutable")
                    return self.public_job(row)
                raise Conflict("terminal result is immutable")
            if row["state"] != State.COLLECTING:
                raise Conflict("backend completion has not been confirmed")
            spec = WorkloadSpec.model_validate(body["spec"])
            problems = []
            if digest != artifact_digest:
                problems.append("ARTIFACT_DIGEST_MISMATCH")
            if (
                result.workload_signature != body["workload_signature"]
                or result.context_signature != body["context_signature"]
            ):
                problems.append("SIGNATURE_MISMATCH")
            if result.evidence_kind == "synthetic" and not self.accept_synthetic:
                problems.append("SYNTHETIC_EVIDENCE_DISABLED")
            if result.measurements and result.measurements.work_units != spec.identity.work_units:
                problems.append("WORK_UNITS_MISMATCH")
            if body.get("training_isolation") and result.outcome == "COMPLETED":
                from .training import validate_receipt

                try:
                    training = validate_receipt(
                        training_receipt, result, body["training_isolation"]
                    )
                except ValueError as exc:
                    raise Rejected("training protection evidence required") from exc
                if not problems:
                    self.store.put(
                        conn,
                        "training_receipt",
                        result.attempt_id,
                        project,
                        training.model_dump(mode="json"),
                    )
            elif training_receipt is not None:
                raise Rejected("training receipt without completed qualified training")
            if body.get("sampling_binding") and result.outcome == "COMPLETED":
                from .sampling import validate_receipt as validate_sampling

                try:
                    sampling = validate_sampling(sampling_receipt, result, body["sampling_binding"])
                except ValueError as exc:
                    raise Rejected("verified sampling receipt required") from exc
                if not problems:
                    self.store.put(
                        conn,
                        "sampling_receipt",
                        result.attempt_id,
                        project,
                        sampling.model_dump(mode="json"),
                    )
            elif sampling_receipt is not None:
                raise Rejected("sampling receipt without a completed sampling-bound workload")
            if body["variant"].get("thermal_policy") and result.outcome == "COMPLETED":
                from .thermal import assess, validate_trace

                try:
                    thermal = validate_trace(thermal_trace, result, body)
                except ValueError as exc:
                    raise Rejected("verified thermal trace required") from exc
                if not problems:
                    self.store.put(
                        conn,
                        "thermal_trace",
                        result.attempt_id,
                        project,
                        thermal.model_dump(mode="json"),
                    )
                    body["thermal_assessment"] = assess(thermal, body["variant"]["thermal_policy"])
            elif thermal_trace is not None:
                raise Rejected("thermal trace without completed qualified telemetry workload")
            if body["variant"].get("load_context_policy") and result.outcome == "COMPLETED":
                from .load_context import summarize, validate_trace

                try:
                    load = validate_trace(load_trace, result, body)
                except ValueError as exc:
                    raise Rejected("verified container load trace required") from exc
                if not problems:
                    self.store.put(
                        conn, "load_trace", result.attempt_id, project, load.model_dump(mode="json")
                    )
                    body["load_context"] = summarize(load)
            elif load_trace is not None:
                raise Rejected("load trace without completed qualified telemetry workload")
            if phase_profile is not None:
                from .diagnostics import validate_profile

                try:
                    phases = validate_profile(
                        phase_profile, result, spec.identity.measurement_boundary
                    )
                except ValueError as exc:
                    raise Rejected("invalid phase profile") from exc
                if not problems:
                    self.store.put(
                        conn,
                        "phase_profile",
                        result.attempt_id,
                        project,
                        phases.model_dump(mode="json"),
                    )
            body["result_digest"] = digest
            body["finished_at"] = now().isoformat()
            body["error"] = ",".join(problems) if problems else result.error_code
            state = (
                State.RESULT_INVALID
                if problems
                else (State.SUCCEEDED if result.outcome == "COMPLETED" else State.FAILED)
            )
            self.store.put(
                conn, "result", result.attempt_id, project, result.model_dump(mode="json")
            )
            body["quality_passed"] = bool(
                result.measurements
                and not problems
                and result.measurements.quality_value >= spec.quality.minimum
                and result.measurements.peak_memory_mib <= spec.quality.maximum_peak_memory_mib
            )
            self.store.change_job(conn, row, state, body)
            if not problems:
                if (
                    result.outcome == "COMPLETED"
                    and body["quality_passed"]
                    and body.get("thermal_assessment", {}).get("status", "ELIGIBLE_TRACE")
                    == "ELIGIBLE_TRACE"
                    and body["request"]["mode"] != "pilot"
                ):
                    self.store.put(
                        conn,
                        "profile",
                        result.attempt_id,
                        project,
                        {
                            "job_id": row["id"],
                            "candidate_ref": body["candidate"]["ref"],
                            "recorded_at": body["finished_at"],
                            "result": result.model_dump(mode="json"),
                        },
                    )
                self.store.enqueue(
                    conn,
                    "artifact-" + result.attempt_id,
                    "artifact",
                    {"job_id": row["id"]},
                )
            return self.public_job(self.store.job(conn, row["id"]))

    def lookup_profiles(self, conn, project, spec, profile_refs=None):
        """Resolve immutable same-workload observations without changing their validity."""
        if profile_refs is None:
            profiles = [
                item
                for item in self.store.list(conn, "profile", project)
                if item["body"]["result"]["workload_signature"] == signature(spec.identity)
            ]
        else:
            profiles = []
            for ref in profile_refs:
                item = self.store.get(conn, "profile", ref)
                if item is None or item["project"] != project:
                    raise NotFound("lookup profile not found")
                if item["body"]["result"]["workload_signature"] != signature(spec.identity):
                    raise Rejected("lookup profile belongs to a different workload signature")
                profiles.append(item)
        return sorted(profiles, key=lambda item: item["ref"])

    @staticmethod
    def lookup_cohort_digest(profiles):
        return signature(
            [{"ref": item["ref"], "digest": signature(item["body"])} for item in profiles]
        )

    def recommend(self, project, workload_ref, *, profile_refs=None, cohort_digest=None):
        with self.store.transaction() as conn:
            spec = WorkloadSpec.model_validate(
                required(self.store, conn, "workload", workload_ref, project)
            )
            profiles = self.lookup_profiles(conn, project, spec, profile_refs)
            if cohort_digest is not None and self.lookup_cohort_digest(profiles) != cohort_digest:
                raise Rejected("lookup profile cohort changed")
            candidates, rejected, drift_evidence = [], {}, {}
            for candidate in spec.candidates:
                try:
                    _, _, variant, cap = self.bundle(conn, project, spec.ref, candidate.ref)
                    errors = compatibility(spec, candidate, variant, cap)
                except NotFound:
                    errors = ["REGISTRY_ENTRY_MISSING"]
                except Rejected:
                    errors = ["QUALIFICATION_REJECTED"]
                if errors:
                    rejected[candidate.ref] = errors
                    continue
                matches = []
                for item in profiles:
                    profile = item["body"]
                    result = profile["result"]
                    age = (now() - datetime.fromisoformat(profile["recorded_at"])).total_seconds()
                    if (
                        result["workload_signature"] == signature(spec.identity)
                        and result["context_signature"] == context_signature(candidate, variant)
                        and result["outcome"] == "COMPLETED"
                        and result["measured"] is True
                        and 0 <= age <= spec.quality.max_profile_age_seconds
                        and (result["evidence_kind"] == "hardware" or self.accept_synthetic)
                    ):
                        matches.append(
                            (item["ref"], result["measurements"], profile["recorded_at"])
                        )
                from .uncertainty import recent_profile_window

                matches, drift_refs = recent_profile_window(matches, spec.quality.minimum_repeats)
                if drift_refs:
                    rejected[candidate.ref] = ["NEEDS_RECONFIRMATION"]
                    drift_evidence[candidate.ref] = drift_refs
                    continue
                if len(matches) < spec.quality.minimum_repeats:
                    rejected[candidate.ref] = ["NEEDS_PROFILE"]
                    continue
                values = [m["elapsed_seconds"] for _, m, _ in matches]
                mean = statistics.mean(values)
                # Descriptive conservative interval, not a calibrated Bayesian posterior.
                radius = (
                    3 * statistics.stdev(values) / math.sqrt(len(values))
                    if len(values) > 1
                    else mean
                )
                candidates.append(
                    {
                        "candidate_ref": candidate.ref,
                        "mean_seconds": mean,
                        "interval_seconds": [max(0, mean - radius), mean + radius],
                        "independent_runs": len(values),
                        "evidence_refs": [ref for ref, _, _ in matches],
                    }
                )
            candidates.sort(key=lambda c: c["mean_seconds"])
            baseline = next(
                (c for c in candidates if c["candidate_ref"] == spec.baseline_candidate_ref), None
            )
            chosen = candidates[0] if candidates else None
            status = "MEASURED_RECOMMENDATION"
            if not chosen:
                status = (
                    "NEEDS_RECONFIRMATION"
                    if any("NEEDS_RECONFIRMATION" in e for e in rejected.values())
                    else "NEEDS_PROFILE"
                    if any("NEEDS_PROFILE" in e for e in rejected.values())
                    else "NO_COMPATIBLE_VARIANT"
                )
            elif not baseline:
                chosen, status = None, "INSUFFICIENT_BASELINE_EVIDENCE"
            elif (
                chosen != baseline
                and chosen["interval_seconds"][1] >= baseline["interval_seconds"][0]
            ):
                chosen, status = baseline, "PRESERVE_BASELINE_UNCERTAINTY"
            rec = {
                "ref": "rec-" + uuid4().hex,
                "workload_ref": spec.ref,
                "workload_digest": signature(spec),
                "status": status,
                "candidate_ref": chosen["candidate_ref"] if chosen else None,
                "measured": bool(chosen),
                "ranking": candidates,
                "excluded": rejected,
                "drift_evidence_refs": drift_evidence,
                "approval_required": True,
                "created_at": now().isoformat(),
                "expires_at": (now() + timedelta(minutes=15)).isoformat(),
                "evidence_policy": (
                    "lookup-v2; last 2*max(3,minimum_repeats) runs; adjacent-block drift gate; "
                    "mean +/- 3 standard errors, not a predictive interval"
                ),
            }
            if profile_refs is not None:
                rec["lookup_history"] = {
                    "profile_refs": [item["ref"] for item in profiles],
                    "cohort_digest": self.lookup_cohort_digest(profiles),
                    "scope": "explicit immutable cohort; current validity checks still apply",
                }
            self.store.put(conn, "recommendation", rec["ref"], project, rec)
            return dict(rec, digest=signature(rec))

    def approve(self, project, ref, request: ApprovalRequest):
        with self.store.transaction() as conn:
            rec = required(self.store, conn, "recommendation", ref, project)
            if (
                signature(rec) != request.recommendation_digest
                or not rec["measured"]
                or rec["candidate_ref"] != request.candidate_ref
                or datetime.fromisoformat(rec["expires_at"]) < now()
            ):
                raise Rejected("recommendation does not match approval or has expired")
            from .uncertainty import assess_recommendation

            assessment = assess_recommendation(self, conn, project, rec)
            if not assessment["reusable"]:
                raise Rejected(
                    "recommendation requires recheck: " + ",".join(assessment["reasons"])
                )
            approval = {
                "ref": "apr-" + uuid4().hex,
                "recommendation_ref": ref,
                "recommendation_digest": signature(rec),
                "candidate_ref": rec["candidate_ref"],
                "workload_digest": rec["workload_digest"],
                "expires_at": rec["expires_at"],
            }
            self.store.put(conn, "approval", approval["ref"], project, approval)
            return approval

    def check_approval_evidence(self, conn, project, approval):
        from .uncertainty import assess_recommendation

        rec = required(self.store, conn, "recommendation", approval["recommendation_ref"], project)
        if signature(rec) != approval["recommendation_digest"]:
            raise Rejected("approval recommendation digest mismatch")
        assessment = assess_recommendation(self, conn, project, rec)
        if not assessment["reusable"]:
            raise Rejected("recommendation requires recheck: " + ",".join(assessment["reasons"]))

    def recommendation_validity(self, project, ref):
        from .uncertainty import assess_recommendation

        with self.store.transaction() as conn:
            rec = required(self.store, conn, "recommendation", ref, project)
            return assess_recommendation(self, conn, project, rec)
