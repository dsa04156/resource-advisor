"""Two real requests sharing one registered execution route; no simulated queue."""

import hashlib

from sqlalchemy import select

from .console import job_view
from .contracts import Contract, JobRequest, Ref, now
from .scheduling import SchedulingPlanRequest, compile_plan
from .service import NotFound, Rejected, required
from .store import jobs


class QueueScenarioRequest(Contract):
    workload_ref: Ref
    profile_ref: Ref


class QueueScenarios:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def start(self, project, request, key):
        if not key or len(key) > 128:
            raise Rejected("Idempotency-Key must contain 1–128 characters")
        ref = "qs-" + hashlib.sha256(f"{project}:{key}".encode()).hexdigest()[:40]
        body = request.model_dump(mode="json")
        with self.store.transaction() as conn:
            existing = self.store.get(conn, "queue_scenario", ref)
            if not existing:
                spec = required(self.store, conn, "workload", request.workload_ref, project)
                # A one-route example demonstrates contention without asking the
                # researcher to choose hardware or changing global queue policy.
                if len(spec["candidates"]) != 1:
                    raise Rejected("Queue scenario requires a single-route registered workload")
                compile_plan(self.service, conn, project, SchedulingPlanRequest(**body))
            self.store.put(conn, "queue_scenario", ref, project, body)
        # Separate durable submissions intentionally allow partial progress.
        # Retrying the same scenario resumes missing jobs without duplicating A.
        for index in range(2):
            self.service.submit(
                project,
                JobRequest(
                    workload_ref=request.workload_ref,
                    scheduling_profile_ref=request.profile_ref,
                    mode="observe",
                ),
                f"{ref}-{index}",
            )
        return self.view(project, ref)

    def view(self, project, ref):
        with self.store.transaction() as conn:
            row = self.store.get(conn, "queue_scenario", ref)
            if not row or row["project"] != project:
                raise NotFound("queue scenario not found")
            result = []
            for index in range(2):
                job = (
                    conn.execute(
                        select(jobs).where(
                            jobs.c.project == project, jobs.c.idempotency_key == f"{ref}-{index}"
                        )
                    )
                    .mappings()
                    .first()
                )
                result.append(job_view(self.service, conn, job) if job else None)
            return {
                "ref": ref,
                **row["body"],
                "created_at": row["created_at"],
                "observed_at": now().isoformat(),
                "jobs": result,
                "semantics": "Two real jobs; backend controls order/admission. Waiting is not guaranteed.",
            }

    def recent(self, project):
        with self.store.transaction() as conn:
            from .store import entities

            rows = conn.execute(
                select(entities)
                .where(entities.c.kind == "queue_scenario", entities.c.project == project)
                .order_by(entities.c.created_at.desc())
                .limit(10)
            ).mappings()
            return {
                "items": [
                    {"ref": r["ref"], **r["body"], "created_at": r["created_at"]} for r in rows
                ]
            }
