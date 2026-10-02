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
    RuntimeVariant,
    State,
    WorkloadSpec,
    now,
    signature,
)
from .policy import compatibility, context_signature
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
    def __init__(self, store, *, accept_synthetic=False):
        self.store = store
        self.accept_synthetic = accept_synthetic

    def register(self, kind, model, project):
        if hasattr(model, "project_ref") and model.project_ref != project:
            raise Rejected("project does not match authenticated principal")
        with self.store.transaction() as conn:
            return self.store.put(conn, kind, model.ref, project, model.model_dump(mode="json"))[
                "body"
            ]

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
        return spec, candidate, variant, cap

    def submit(self, project: str, request: JobRequest, key: str):
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
                    if existing["request_digest"] != signature(request):
                        raise Conflict("idempotency key reused with different request")
                    return self.public_job(existing)
                spec, candidate, variant, cap = self.bundle(
                    conn, project, request.workload_ref, request.candidate_ref
                )
                errors = compatibility(spec, candidate, variant, cap)
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
                job_id, attempt_id = "j-" + uuid4().hex, "a-" + uuid4().hex
                body = {
                    "request": request.model_dump(mode="json"),
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
                if spec.identity.task_type == "training":
                    body["training_isolation"] = required(
                        self.store, conn, "training_isolation", spec.ref, project
                    )
                conn.execute(
                    insert(jobs).values(
                        id=job_id,
                        project=project,
                        idempotency_key=key,
                        request_digest=signature(request),
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
                if row and row["request_digest"] == signature(request):
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
                body = dict(row["body"])
                body.setdefault("cancel_before_submit", row["state"] == State.VALIDATED)
                self.store.change_job(conn, row, State.CANCEL_REQUESTED, body)
                self.store.enqueue(
                    conn, "cancel-" + row["body"]["attempt_id"], "cancel", {"job_id": job_id}
                )
            return self.public_job(self.store.job(conn, job_id))

    def ingest(
        self,
        project,
        result: ExecutionResult,
        artifact_digest: str,
        *,
        phase_profile=None,
        training_receipt=None,
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

    def recommend(self, project, workload_ref):
        with self.store.transaction() as conn:
            spec = WorkloadSpec.model_validate(
                required(self.store, conn, "workload", workload_ref, project)
            )
            profiles = self.store.list(conn, "profile", project)
            candidates, rejected = [], {}
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
                        matches.append((item["ref"], result["measurements"]))
                if len(matches) < spec.quality.minimum_repeats:
                    rejected[candidate.ref] = ["NEEDS_PROFILE"]
                    continue
                values = [m["elapsed_seconds"] for _, m in matches]
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
                        "evidence_refs": [ref for ref, _ in matches],
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
                    "NEEDS_PROFILE"
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
                "approval_required": True,
                "created_at": now().isoformat(),
                "expires_at": (now() + timedelta(minutes=15)).isoformat(),
                "evidence_policy": "lookup-v1; independent runs; mean +/- 3 standard errors",
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
