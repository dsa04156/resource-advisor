"""API credentials are supplied externally; project identity is server-derived."""

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from sqlalchemy import func, select

from .contracts import (
    ApprovalRequest,
    CapabilitySnapshot,
    ExecutionResult,
    JobRequest,
    JobTemplate,
    RecommendationRequest,
    RuntimeVariant,
    StudyRequest,
    WorkloadSpec,
)
from .qualifications import QualificationImport, Qualifications
from .qualifications import list_page as qualification_page
from .scheduling import SchedulingPlanRequest, SchedulingProfile, compile_plan
from .service import NotFound, Rejected, Service
from .store import Conflict, jobs, outbox, usage

PREFIX = "/api/v1/compute"



@dataclass(frozen=True)
class Principal:
    project: str
    operator: bool = False


def create_app(
    service: Service,
    credentials: dict[str, Principal],
    *,
    artifact_storage=None,
    anonymous_project=None,
):
    if not credentials:
        raise ValueError("at least one external credential hash is required")
    if anonymous_project is not None and anonymous_project not in {
        identity.project for identity in credentials.values()
    }:
        raise ValueError("anonymous project must be an existing configured project")
    app = FastAPI(title="Resource Advisor", version="0.1.0")
    from .study import Studies

    study_service = Studies(service)
    model_qualifications = Qualifications(service.store)

    def principal(authorization: str = Header(default="")):
        if not authorization and anonymous_project is not None:
            return Principal(anonymous_project)
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required")
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        for expected, identity in credentials.items():
            if hmac.compare_digest(digest, expected):
                return identity
        raise HTTPException(401, "Invalid token")

    def operator(p=Depends(principal)):
        if not p.operator:
            raise HTTPException(403, "Operator qualification required")
        return p

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(Rejected)
    async def rejected(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(NotFound)
    async def missing(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith((PREFIX, "/console")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/console"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'"
            )
        return response

    @app.get("/console", include_in_schema=False)
    def console_shell():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/console/session", include_in_schema=False)
    def console_session():
        return {
            "authentication_required": anonymous_project is None,
            "project_ref": anonymous_project,
        }

    @app.get("/console/{asset}", include_in_schema=False)
    def console_asset(asset: str):
        if asset not in {"console.js", "console.css", "mark.svg"}:
            raise HTTPException(404, "Asset not found")
        return FileResponse(Path(__file__).parent / "static" / asset)

    @app.get(PREFIX + "/overview")
    def console_overview(
        jobs_page: int = Query(default=0, ge=0, le=100000),
        compatibility_page: int = Query(default=0, ge=0, le=100000),
        history_page: int = Query(default=0, ge=0, le=100000),
        recommendations_page: int = Query(default=0, ge=0, le=100000),
        templates_page: int = Query(default=0, ge=0, le=100000),
        qualifications_page: int = Query(default=0, ge=0, le=100000),
        jobs_status: Literal[
            "all", "running", "pending", "succeeded", "failed", "canceled"
        ] = "all",
        jobs_backend: Literal["all", "kubernetes", "slurm"] = "all",
        jobs_search: str = Query(default="", max_length=128),
        p=Depends(principal),
    ):
        from .console import overview

        return overview(
            service,
            p.project,
            jobs_page=jobs_page,
            compatibility_page=compatibility_page,
            history_page=history_page,
            recommendations_page=recommendations_page,
            qualifications_page=qualifications_page,
            templates_page=templates_page,
            jobs_status=jobs_status,
            jobs_backend=jobs_backend,
            jobs_search=jobs_search,
        )

    @app.get(PREFIX + "/recommendations/{ref}/evidence")
    def console_evidence(ref: str, p=Depends(principal)):
        from .console import recommendation_evidence

        return recommendation_evidence(service, p.project, ref)

    @app.get(PREFIX + "/recommendations/{ref}/validity")
    def recommendation_validity(ref: str, p=Depends(principal)):
        return service.recommendation_validity(p.project, ref)

    @app.get(PREFIX + "/uncertainty/shadow-report")
    def uncertainty_report(p=Depends(principal)):
        from .uncertainty import shadow_report

        return shadow_report(service.store, p.project)

    @app.get("/healthz")
    def health():
        with service.store.transaction() as conn:
            conn.execute(select(1))
        return {"status": "ok", "execution_enabled": "worker configuration required"}

    @app.post(PREFIX + "/scheduling-profiles")
    def register_scheduling_profile(value: SchedulingProfile, p=Depends(operator)):
        return service.register("scheduling_profile", value, p.project)

    @app.get(PREFIX + "/scheduling-profiles")
    def scheduling_profiles(p=Depends(principal)):
        with service.store.transaction() as conn:
            return {
                "items": [
                    row["body"] for row in service.store.list(conn, "scheduling_profile", p.project)
                ]
            }

    @app.post(PREFIX + "/scheduling-plans")
    def scheduling_plan(value: SchedulingPlanRequest, p=Depends(principal)):
        with service.store.transaction() as conn:
            try:
                return compile_plan(service, conn, p.project, value)
            except (NotFound, Rejected):
                raise
            except ValueError as exc:
                raise Rejected(str(exc)) from exc

    @app.post(PREFIX + "/profiling-runs")
    def create_study(value: StudyRequest, idempotency_key: str = Header(), p=Depends(principal)):
        return study_service.create(p.project, value, idempotency_key)

    @app.get(PREFIX + "/profiling-runs/{ref}")
    def get_study(ref: str, p=Depends(principal)):
        return study_service.get(p.project, ref)

    @app.post(PREFIX + "/profiling-runs/{ref}/cancel")
    def cancel_study(ref: str, p=Depends(principal)):
        return study_service.cancel(p.project, ref)

    from .fidelity import FidelityCalibration, FidelityPlan

    fidelity = FidelityCalibration(service)

    @app.post(PREFIX + "/fidelity-plans")
    def create_fidelity_plan(value: FidelityPlan, p=Depends(operator)):
        return fidelity.create(p.project, value)

    @app.get(PREFIX + "/fidelity-plans/{ref}")
    def get_fidelity_plan(ref: str, p=Depends(principal)):
        return fidelity.get(p.project, ref)

    @app.post(PREFIX + "/fidelity-plans/{ref}/slots/{slot}")
    def submit_fidelity_slot(ref: str, slot: int, p=Depends(principal)):
        return fidelity.submit_slot(p.project, ref, slot)

    @app.get(PREFIX + "/fidelity-plans/{ref}/assessment")
    def fidelity_assessment(ref: str, p=Depends(principal)):
        return fidelity.assess(p.project, ref)

    from .fidelity_space import FidelityEvidenceRequest, FidelitySpace, FidelitySpaces

    fidelity_spaces = FidelitySpaces(service)

    from .transfer import TransferEvidenceRequest, TransferSpace, TransferSpaces

    transfer_spaces = TransferSpaces(service)

    @app.post(PREFIX + "/transfer-spaces")
    def create_transfer_space(value: TransferSpace, p=Depends(operator)):
        return transfer_spaces.create(p.project, value)

    @app.get(PREFIX + "/transfer-spaces/{ref}")
    def get_transfer_space(ref: str, p=Depends(principal)):
        return transfer_spaces.get(p.project, ref)

    @app.post(PREFIX + "/transfer-spaces/{ref}/evidence")
    def bind_transfer_evidence(ref: str, value: TransferEvidenceRequest, p=Depends(principal)):
        return transfer_spaces.evidence(p.project, ref, value)

    @app.get(PREFIX + "/transfer-spaces/{ref}/evidence/{evidence_ref}")
    def get_transfer_evidence(ref: str, evidence_ref: str, p=Depends(principal)):
        with service.store.transaction() as conn:
            _, evidence = transfer_spaces.checked(conn, p.project, ref, evidence_ref)
        return evidence

    from .fidelity_qualification import FidelityQualificationPlan, FidelityQualifications

    qualifications = FidelityQualifications(service)

    @app.post(PREFIX + "/fidelity-qualifications")
    def create_fidelity_qualification(value: FidelityQualificationPlan, p=Depends(operator)):
        return qualifications.create(p.project, value)

    @app.get(PREFIX + "/fidelity-qualifications/{ref}")
    def get_fidelity_qualification(ref: str, p=Depends(principal)):
        return qualifications.get(p.project, ref)

    @app.post(PREFIX + "/fidelity-qualifications/{ref}/assessment")
    def assess_fidelity_qualification(ref: str, p=Depends(principal)):
        return qualifications.assess(p.project, ref)

    @app.get(PREFIX + "/fidelity-qualifications/{ref}/assessment")
    def fidelity_qualification_status(ref: str, p=Depends(principal)):
        return qualifications.status(p.project, ref)

    @app.post(PREFIX + "/fidelity-spaces")
    def create_fidelity_space(value: FidelitySpace, p=Depends(operator)):
        return fidelity_spaces.create(p.project, value)

    @app.get(PREFIX + "/fidelity-spaces/{ref}")
    def get_fidelity_space(ref: str, p=Depends(principal)):
        return fidelity_spaces.get(p.project, ref)

    @app.get(PREFIX + "/fidelity-spaces/{ref}/options/{option_ref}")
    def resolve_fidelity_option(ref: str, option_ref: str, p=Depends(principal)):
        return fidelity_spaces.resolve(p.project, ref, option_ref)

    @app.post(PREFIX + "/fidelity-spaces/{ref}/evidence")
    def bind_fidelity_evidence(ref: str, value: FidelityEvidenceRequest, p=Depends(principal)):
        return fidelity_spaces.evidence(p.project, ref, value)

    @app.post(PREFIX + "/capabilities")
    def register_capability(value: CapabilitySnapshot, p=Depends(operator)):
        return service.register("capability", value, p.project)

    @app.get(PREFIX + "/capabilities")
    def capabilities(p=Depends(principal)):
        with service.store.transaction() as conn:
            return [r["body"] for r in service.store.list(conn, "capability", p.project)]

    @app.get(PREFIX + "/inventory/{cluster_ref}")
    def inventory(cluster_ref: str, p=Depends(principal)):
        from .inventory import latest_inventory

        snapshot = latest_inventory(service.store, p.project, cluster_ref)
        if snapshot is None:
            raise HTTPException(404, "Inventory not found")
        return snapshot

    @app.post(PREFIX + "/variants")
    def register_variant(value: RuntimeVariant, p=Depends(operator)):
        return service.register("variant", value, p.project)

    @app.post(PREFIX + "/qualifications")
    def import_qualification(value: QualificationImport, p=Depends(operator)):
        return model_qualifications.create(p.project, value)

    @app.get(PREFIX + "/qualifications")
    def list_qualifications(page: int = Query(default=0, ge=0, le=100000), p=Depends(principal)):
        with service.store.transaction() as conn:
            return qualification_page(service.store, conn, p.project, page)

    @app.get(PREFIX + "/qualifications/{ref}")
    def get_qualification(ref: str, p=Depends(principal)):
        return model_qualifications.get(p.project, ref)

    @app.post(PREFIX + "/job-templates")
    def register_job_template(value: JobTemplate, p=Depends(principal)):
        return service.register_template(p.project, value)

    @app.post(PREFIX + "/workloads")
    def register_workload(value: WorkloadSpec, p=Depends(principal)):
        return service.register("workload", value, p.project)

    from .sampling import SamplingBindingRequest, SamplingPolicies, SamplingPolicy

    @app.post(PREFIX + "/sampling-policies")
    def register_sampling_policy(value: SamplingPolicy, p=Depends(operator)):
        return service.register("sampling_policy", value, p.project)

    @app.get(PREFIX + "/sampling-policies/{policy_ref}")
    def get_sampling_policy(policy_ref: str, p=Depends(principal)):
        from .service import required

        with service.store.transaction() as conn:
            return required(service.store, conn, "sampling_policy", policy_ref, p.project)

    @app.post(PREFIX + "/sampling-bindings")
    def bind_sampling(value: SamplingBindingRequest, p=Depends(operator)):
        return SamplingPolicies(service).bind(p.project, value)

    @app.get(PREFIX + "/jobs/{job_id}/sampling-receipt")
    def get_sampling_receipt(job_id: str, p=Depends(principal)):
        job = service.get_job(p.project, job_id)
        with service.store.transaction() as conn:
            receipt = service.store.get(conn, "sampling_receipt", job["attempt_id"])
        if receipt is None:
            raise HTTPException(404, "Sampling receipt not available")
        return receipt["body"]

    @app.get(PREFIX + "/jobs/{job_id}/thermal-trace")
    def get_thermal_trace(job_id: str, p=Depends(principal)):
        job = service.get_job(p.project, job_id)
        with service.store.transaction() as conn:
            trace = service.store.get(conn, "thermal_trace", job["attempt_id"])
            row = service.store.job(conn, job_id)
        if trace is None:
            raise HTTPException(404, "Thermal trace not available")
        return {"trace": trace["body"], "assessment": row["body"]["thermal_assessment"]}

    from .training import TrainingIsolation

    @app.post(PREFIX + "/training-isolation")
    def register_training_isolation(value: TrainingIsolation, p=Depends(operator)):
        return service.register("training_isolation", value, p.project)

    @app.get(PREFIX + "/jobs/{job_id}/training-receipt")
    def training_receipt(job_id: str, p=Depends(principal)):
        job = service.get_job(p.project, job_id)
        with service.store.transaction() as conn:
            row = service.store.get(conn, "training_receipt", job["attempt_id"])
        if row is None:
            raise HTTPException(404, "Training receipt not available")
        return row["body"]

    @app.post(PREFIX + "/jobs")
    def submit(value: JobRequest, idempotency_key: str = Header(), p=Depends(principal)):
        return service.submit(p.project, value, idempotency_key)

    @app.get(PREFIX + "/jobs/{job_id}")
    def job(job_id: str, p=Depends(principal)):
        return service.get_job(p.project, job_id)

    @app.get(PREFIX + "/jobs")
    def list_jobs(p=Depends(principal)):
        with service.store.transaction() as conn:
            rows = conn.execute(
                select(jobs).where(jobs.c.project == p.project).limit(200)
            ).mappings()
            return [service.public_job(row) for row in rows]

    @app.get(PREFIX + "/jobs/{job_id}/diagnostics")
    def diagnostics(job_id: str, p=Depends(principal)):
        from .diagnostics import diagnose

        job = service.get_job(p.project, job_id)
        with service.store.transaction() as conn:
            phases = service.store.get(conn, "phase_profile", job["attempt_id"])
            result = service.store.get(conn, "result", job["attempt_id"])
        return diagnose(
            phases["body"] if phases else None,
            evidence_kind=result["body"]["evidence_kind"] if result else None,
        )

    @app.get(PREFIX + "/jobs/{job_id}/load-context")
    def load_context(job_id: str, p=Depends(principal)):
        job = service.get_job(p.project, job_id)
        with service.store.transaction() as conn:
            load = service.store.get(conn, "load_trace", job["attempt_id"])
            row = service.store.job(conn, job_id)
        return {
            "trace": load["body"] if load else None,
            "summary": row["body"].get("load_context"),
            "status": "RECORDED" if load else "NOT_MEASURED",
        }

    @app.get(PREFIX + "/jobs/{job_id}/artifacts")
    def artifacts(job_id: str, p=Depends(principal)):
        service.get_job(p.project, job_id)
        with service.store.transaction() as conn:
            return [
                {k: v for k, v in row["body"].items() if k not in {"bucket", "key"}}
                for row in service.store.list(conn, "artifact", p.project)
                if row["body"]["job_id"] == job_id
            ]

    @app.get(PREFIX + "/artifacts/{ref}/content")
    def artifact_content(ref: str, p=Depends(principal)):
        with service.store.transaction() as conn:
            row = service.store.get(conn, "artifact", ref)
            if not row or row["project"] != p.project:
                raise HTTPException(404, "Artifact not found")
        if artifact_storage is None:
            raise HTTPException(503, "Artifact storage not configured")
        from botocore.exceptions import BotoCoreError, ClientError

        from .artifacts import ArtifactError

        try:
            data = artifact_storage.read(row["body"])
        except (ArtifactError, BotoCoreError, ClientError):
            raise HTTPException(503, "Artifact unavailable or integrity check failed") from None
        return Response(
            data,
            media_type="application/json",
            headers={"ETag": '"' + row["body"]["digest"] + '"'},
        )

    @app.post(PREFIX + "/jobs/{job_id}/cancel")
    def cancel(job_id: str, p=Depends(principal)):
        return service.cancel(p.project, job_id)

    @app.post(PREFIX + "/jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str, p=Depends(principal)):
        return service.heartbeat(p.project, job_id)

    @app.post(PREFIX + "/results")
    def result(value: ExecutionResult, x_artifact_digest: str = Header(), p=Depends(operator)):
        return service.ingest(p.project, value, x_artifact_digest)

    @app.get(PREFIX + "/profiles")
    def profiles(p=Depends(principal)):
        with service.store.transaction() as conn:
            return [r["body"] for r in service.store.list(conn, "profile", p.project)]

    @app.post(PREFIX + "/recommendations")
    def recommend(value: RecommendationRequest, p=Depends(principal)):
        return service.recommend(p.project, value.workload_ref)

    @app.post(PREFIX + "/recommendations/{ref}/approve")
    def approve(ref: str, value: ApprovalRequest, p=Depends(principal)):
        return service.approve(p.project, ref, value)

    @app.get(PREFIX + "/usage")
    def accounting(p=Depends(principal)):
        with service.store.transaction() as conn:
            return [
                dict(r)
                for r in conn.execute(select(usage).where(usage.c.project == p.project)).mappings()
            ]

    @app.get(PREFIX + "/usage/summary")
    def accounting_summary(p=Depends(principal)):
        from .accounting import summarize

        with service.store.transaction() as conn:
            records = conn.execute(select(usage).where(usage.c.project == p.project)).mappings()
            return {
                "groups": summarize(records),
                "semantics": "Known reservations only; unknown and legacy counts are explicit. Units from different device classes/modes are not combined.",
            }

    @app.get("/metrics")
    def metrics(p=Depends(operator)):
        registry = CollectorRegistry()
        gauge = Gauge(
            "resource_advisor_jobs", "Jobs by project and state", ["state"], registry=registry
        )
        pending = Gauge(
            "resource_advisor_outbox_pending",
            "Undelivered events (operator global)",
            ["kind"],
            registry=registry,
        )
        with service.store.transaction() as conn:
            for state, count in conn.execute(
                select(jobs.c.state, func.count())
                .where(jobs.c.project == p.project)
                .group_by(jobs.c.state)
            ):
                gauge.labels(state).set(count)
            for kind, count in conn.execute(
                select(outbox.c.kind, func.count())
                .where(outbox.c.status != "DONE")
                .group_by(outbox.c.kind)
            ):
                pending.labels(kind).set(count)
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    return app
