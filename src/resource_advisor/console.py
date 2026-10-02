"""Project-scoped read models for four evidence views; never schedules work."""

from datetime import datetime

from sqlalchemy import func, select

from .contracts import WorkloadSpec, now, signature
from .diagnostics import diagnose
from .inventory import fresh_view
from .policy import compatibility
from .service import NotFound, Rejected
from .store import entities, jobs, usage

PAGE_SIZE = 25


def page_rows(conn, query, page):
    total = conn.execute(
        select(func.count()).select_from(query.order_by(None).subquery())
    ).scalar_one()
    rows = list(conn.execute(query.offset(page * PAGE_SIZE).limit(PAGE_SIZE)).mappings())
    return rows, {
        "page": page,
        "size": PAGE_SIZE,
        "total": total,
        "has_next": (page + 1) * PAGE_SIZE < total,
    }


def entity_query(kind, project):
    return (
        select(entities)
        .where(entities.c.kind == kind, entities.c.project == project)
        .order_by(entities.c.created_at.desc(), entities.c.ref.desc())
    )


def owned(store, conn, project, kind, ref):
    row = store.get(conn, kind, ref) if ref else None
    return row["body"] if row and row["project"] == project else None


def job_view(service, conn, row):
    body = row["body"]
    result = owned(service.store, conn, row["project"], "result", body["attempt_id"])
    tracking = owned(service.store, conn, row["project"], "tracking", body["attempt_id"])
    phases = owned(service.store, conn, row["project"], "phase_profile", body["attempt_id"])
    return {
        **service.public_job(row),
        "workload_ref": body["request"]["workload_ref"],
        "candidate_ref": body["candidate"]["ref"],
        "backend": body["candidate"]["backend"],
        "cluster_ref": body["backend_cluster_id"],
        "node_ref": body["capability"]["node_ref"],
        "mode": body["request"]["mode"],
        "requested_resources": body["candidate"]["context"]["resources"],
        "allocation_mode": body["candidate"]["context"]["allocation_mode"],
        "observed_allocation": body.get("allocation"),
        "quality_passed": body.get("quality_passed"),
        "result": result,
        "tracking": tracking,
        "diagnostics": diagnose(phases, evidence_kind=result["evidence_kind"] if result else None),
        **{k: body.get(k) for k in ("started_at", "finished_at", "backend_observed_at")},
    }


def selected_context(store, conn, project, rec):
    spec = owned(store, conn, project, "workload", rec["workload_ref"])
    if not spec or signature(spec) != rec["workload_digest"]:
        return None
    candidate = next((c for c in spec["candidates"] if c["ref"] == rec.get("candidate_ref")), None)
    if candidate is None:
        return None
    variant = owned(store, conn, project, "variant", candidate["variant_ref"])
    context = candidate["context"]
    return {
        "backend": candidate["backend"],
        "accelerator_model": context["accelerator_model"],
        "resources": context["resources"],
        "allocation_mode": context["allocation_mode"],
        "device_class": variant["device_class"] if variant else "unknown",
    }


def overview(
    service, project, *, jobs_page=0, compatibility_page=0, history_page=0, recommendations_page=0
):
    store = service.store
    with store.transaction() as conn:
        # Rank in SQL so polling does not read the entire append-only inventory history.
        cluster = entities.c.body["cluster_ref"].as_string()
        ranked = (
            select(
                entities.c.ref,
                func.row_number()
                .over(
                    partition_by=cluster,
                    order_by=(entities.c.created_at.desc(), entities.c.ref.desc()),
                )
                .label("rank"),
            )
            .where(entities.c.kind == "inventory", entities.c.project == project)
            .subquery()
        )
        snapshots = conn.execute(
            select(entities.c.body)
            .join(ranked, entities.c.ref == ranked.c.ref)
            .where(
                entities.c.kind == "inventory", entities.c.project == project, ranked.c.rank == 1
            )
        ).scalars()
        inventory = sorted((fresh_view(s) for s in snapshots), key=lambda s: s["cluster_ref"])
        job_query = (
            select(jobs)
            .where(jobs.c.project == project)
            .order_by(jobs.c.body["created_at"].as_string().desc(), jobs.c.id.desc())
        )
        rows, job_page = page_rows(conn, job_query, jobs_page)
        job_page["items"] = [job_view(service, conn, r) for r in rows]
        counts = dict(
            conn.execute(
                select(jobs.c.state, func.count())
                .where(jobs.c.project == project)
                .group_by(jobs.c.state)
            ).all()
        )
        rows, compat_page = page_rows(conn, entity_query("workload", project), compatibility_page)
        compat_page["items"] = []
        for row in rows:
            spec = WorkloadSpec.model_validate(row["body"])
            candidates = []
            for candidate in spec.candidates:
                base = {
                    "candidate_ref": candidate.ref,
                    "backend": candidate.backend,
                    "variant_ref": candidate.variant_ref,
                    "capability_ref": candidate.capability_ref,
                }
                try:
                    _, _, variant, cap = service.bundle(conn, project, spec.ref, candidate.ref)
                    base.update(
                        node_ref=cap.node_ref,
                        cluster_ref=cap.backend_cluster_id,
                        model=cap.accelerator_model,
                        device_class=cap.device_class,
                        allocation_mode=cap.allocation_mode,
                        verification=variant.verification,
                        runtime_versions=variant.runtime_versions,
                        validation_refs=list(variant.validation_refs),
                        capability_observed_at=cap.observed_at.isoformat(),
                        reasons=compatibility(spec, candidate, variant, cap),
                    )
                except NotFound:
                    base["reasons"] = ["REGISTRY_ENTRY_MISSING"]
                except Rejected:
                    base["reasons"] = ["QUALIFICATION_REJECTED"]
                base["contract_compatible_now"] = not base["reasons"]
                candidates.append(base)
            compat_page["items"].append(
                {
                    "workload_ref": spec.ref,
                    "task_type": spec.identity.task_type,
                    "precision": spec.identity.precision,
                    "candidates": candidates,
                }
            )
        history_query = (
            select(usage)
            .where(usage.c.project == project)
            .order_by(
                usage.c.body["terminal_observed_at"].as_string().desc(), usage.c.attempt_id.desc()
            )
        )
        rows, history = page_rows(conn, history_query, history_page)
        history["items"] = [dict(r) for r in rows]
        rows, recommendations = page_rows(
            conn, entity_query("recommendation", project), recommendations_page
        )
        recommendations["items"] = [
            {**r["body"], "selected_context": selected_context(store, conn, project, r["body"])}
            for r in rows
        ]
    return {
        "generated_at": now().isoformat(),
        "project_ref": project,
        "inventory": inventory,
        "job_counts": counts,
        "jobs": job_page,
        "compatibility": compat_page,
        "history": history,
        "recommendations": recommendations,
        "semantics": "Recorded state; inventory is not model qualification or admission. Null is unknown.",
    }


def recommendation_evidence(service, project, ref):
    store = service.store
    with store.transaction() as conn:
        rec = owned(store, conn, project, "recommendation", ref)
        if rec is None:
            raise NotFound("recommendation not found")
        evidence = []
        evidence_refs = {
            r for rank in rec.get("ranking", []) for r in rank.get("evidence_refs", [])
        }
        for attempt in sorted(evidence_refs):
            result = owned(store, conn, project, "result", attempt)
            evidence.append(
                {
                    "attempt_id": attempt,
                    "result": result,
                    "tracking": owned(store, conn, project, "tracking", attempt),
                }
            )
        approvals = select(entities.c.ref).where(
            entities.c.kind == "approval",
            entities.c.project == project,
            entities.c.body["recommendation_ref"].as_string() == ref,
        )
        rows = (
            conn.execute(
                select(jobs)
                .where(
                    jobs.c.project == project,
                    jobs.c.body["request"]["approval_ref"].as_string().in_(approvals),
                )
                .order_by(jobs.c.body["created_at"].as_string(), jobs.c.id)
                .limit(201)
            )
            .mappings()
            .all()
        )
        comparisons = []
        ranking = {r["candidate_ref"]: r for r in rec.get("ranking", [])}
        for row in rows[:200]:
            job = job_view(service, conn, row)
            rank = ranking.get(job["candidate_ref"])
            result = job["result"]
            approval = owned(
                store, conn, project, "approval", row["body"]["request"].get("approval_ref")
            )
            authorized_configuration = bool(
                approval
                and approval.get("recommendation_digest") == signature(rec)
                and approval.get("workload_digest") == rec["workload_digest"]
                and signature(row["body"]["spec"]) == rec["workload_digest"]
                and approval.get("candidate_ref")
                == job["candidate_ref"]
                == rec.get("candidate_ref")
                and rec["created_at"] <= job["created_at"] <= approval.get("expires_at", "")
            )
            eligible = (
                authorized_configuration
                and result is not None
                and result["job_id"] == job["job_id"]
                and result["outcome"] == "COMPLETED"
                and result["evidence_kind"] == "hardware"
                and job["quality_passed"] is True
                and job["state"] == "SUCCEEDED"
                and job["attempt_id"] not in evidence_refs
            )
            actual = result["measurements"]["elapsed_seconds"] if eligible else None
            expected = rank["mean_seconds"] if rank else None
            comparisons.append(
                {
                    "job": job,
                    "historical_mean_seconds": expected,
                    "actual_seconds": actual,
                    "signed_error_seconds": actual - expected
                    if actual is not None and expected is not None
                    else None,
                    "comparison_status": "independent_measured"
                    if actual is not None
                    else "not_comparable",
                }
            )
        return {
            "recommendation": rec,
            "expired": datetime.fromisoformat(rec["expires_at"]) < now(),
            "evidence_runs": evidence,
            "approved_executions": comparisons,
            "approved_executions_truncated": len(rows) > 200,
            "estimate_kind": "historical measured summary, not a calibrated predictive guarantee",
        }
