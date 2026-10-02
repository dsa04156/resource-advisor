"""API credentials are supplied externally; project identity is server-derived."""

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from sqlalchemy import func, select

from .contracts import (
    ApprovalRequest,
    CapabilitySnapshot,
    ExecutionResult,
    JobRequest,
    RecommendationRequest,
    RuntimeVariant,
    StudyRequest,
    WorkloadSpec,
)
from .service import NotFound, Rejected, Service
from .store import Conflict, jobs, outbox, usage

PREFIX = "/api/v1/compute"


@dataclass(frozen=True)
class Principal:
    project: str
    operator: bool = False


def create_app(service: Service, credentials: dict[str, Principal], *, artifact_storage=None):
    if not credentials:
        raise ValueError("at least one external credential hash is required")
    app = FastAPI(title="Resource Advisor", version="0.1.0")
    from .study import Studies

    study_service = Studies(service)

    def principal(authorization: str = Header(default="")):
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

    @app.post(PREFIX + "/profiling-runs")
    def create_study(value: StudyRequest, idempotency_key: str = Header(), p=Depends(principal)):
        return study_service.create(p.project, value, idempotency_key)

    @app.get(PREFIX + "/profiling-runs/{ref}")
    def get_study(ref: str, p=Depends(principal)):
        return study_service.get(p.project, ref)

    @app.post(PREFIX + "/profiling-runs/{ref}/cancel")
    def cancel_study(ref: str, p=Depends(principal)):
        return study_service.cancel(p.project, ref)

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

    @app.post(PREFIX + "/workloads")
    def register_workload(value: WorkloadSpec, p=Depends(principal)):
        return service.register("workload", value, p.project)

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
