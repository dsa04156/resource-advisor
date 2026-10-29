"""Read-only projections over recorded JCT comparison evidence.

Nothing here evaluates a policy or estimates time: comparison objects are the
stored frozen comparisons, and readiness only counts evidence rows the
recommender would match, split by their own validity windows.
"""

from datetime import datetime

from .contracts import WorkloadSpec, now, signature
from .recommendation import _comparison_view


def list_comparisons(service, project, workload_ref=None):
    with service.store.transaction() as conn:
        rows = [
            x
            for x in service.store.list(conn, "recommendation_comparison", project)
            if workload_ref is None or x["body"]["workload_ref"] == workload_ref
        ]
        rows.sort(key=lambda x: x["body"]["frozen_at"], reverse=True)
        items = [_comparison_view(service, conn, project, x) for x in rows]
    return {"workload_ref": workload_ref, "count": len(items), "items": items}


ARMS = ("static", "profile_only", "profile_queue", "profile_queue_network")
DEFAULT_QUEUE_AGE = 30  # RecommendationPolicy.max_queue_age_seconds default


def _age(at, value):
    return (at - datetime.fromisoformat(value)).total_seconds()


def _accelerators(rows):
    accel = [c for c in rows if c.get("device_class") in {"gpu", "npu"}]
    return (
        sorted({c["device_class"] for c in accel}),
        sorted({c["model"] for c in accel if c.get("model")}),
    )


def _summary(spec, rows, catalog):
    classes, models = _accelerators(rows)
    i = spec.identity
    return {
        "workload_ref": spec.ref,
        "task_type": i.task_type,
        "precision": i.precision,
        "batch_size": i.batch_size,
        "input_shape": list(i.input_shape),
        "dataset_version": i.dataset_version,
        "measurement_boundary": i.measurement_boundary,
        "model_digest": i.model_digest,
        "identity_signature": signature(i),
        "in_submission_catalog": spec.ref in catalog,
        "device_classes": classes,
        "accelerator_models": models,
        "registered": len(rows),
        "submission_compatible": sum(1 for c in rows if c["submittable_now"]),
    }


def _readiness(store, conn, project, spec, row, at, queue_age):
    node, cluster, backend = row.get("node_ref"), row.get("cluster_ref"), str(row["backend"])
    sig = signature(spec.identity)
    profiles = {"fresh": 0, "stale": 0, "unusable": 0, "latest_measured_at": None}
    for x in store.list(conn, "jct_profile", project):
        p = x["body"]
        if p["workload_signature"] != sig or p["candidate_ref"] != row["candidate_ref"]:
            continue
        if p.get("synthetic") or not p.get("quality_passed", True):
            profiles["unusable"] += 1
        elif _age(at, p["measured_at"]) <= p["valid_for_seconds"]:
            profiles["fresh"] += 1
        else:
            profiles["stale"] += 1
        profiles["latest_measured_at"] = max(
            filter(None, [profiles["latest_measured_at"], p["measured_at"]])
        )
    snapshots = [
        x["body"]
        for x in store.list(conn, "route_queue_snapshot", project)
        if x["body"]["backend"] == backend
        and x["body"]["cluster_ref"] == cluster
        and x["body"]["route_ref"] == node
        and x["body"].get("resource_key") == row.get("resource_key")
    ]
    queue = None
    if snapshots:
        q = max(snapshots, key=lambda b: b["observed_at"])
        age = _age(at, q["observed_at"])
        queue = {
            "ref": q["ref"],
            "observed_at": q["observed_at"],
            "age_seconds": age,
            "max_age_seconds": queue_age,
            "stale": age > queue_age,
            "complete": q["complete"],
            "incomplete_reasons": q.get("incomplete_reasons", []),
            "running": len(q.get("running", [])),
            "pending": len(q.get("pending", [])),
            "capacity": q["capacity"],
        }
    network = {"fresh": 0, "stale": 0, "latest_observed_at": None}
    for x in store.list(conn, "network_profile", project):
        n = x["body"]
        if (n["workload_ref"], n["backend"], n["cluster_ref"], n["node_ref"]) != (
            spec.ref,
            backend,
            cluster,
            node,
        ):
            continue
        fresh = _age(at, n["observed_at"]) <= n["valid_for_seconds"]
        network["fresh" if fresh else "stale"] += 1
        network["latest_observed_at"] = max(
            filter(None, [network["latest_observed_at"], n["observed_at"]])
        )
    caches = sum(
        1
        for x in store.list(conn, "cache_binding", project)
        if (x["body"]["backend"], x["body"]["cluster_ref"], x["body"]["node_ref"])
        == (backend, cluster, node)
    )
    return {
        "jct_profiles": profiles,
        "queue": queue,
        "network_profiles": network,
        "cache_bindings": caches,
    }


def _node_observation(inventory, row):
    """Inventory view of the candidate's node, independent of the submission contract.

    Matched by backend and node name: capability cluster refs are logical and need
    not equal the inventory cluster. Absent or stale observations stay unknown.
    """
    node, backend = row.get("node_ref"), str(row["backend"])
    views = [s for s in inventory if s.get("backend", "kubernetes") == backend]
    if not views:
        return {"status": "INVENTORY_UNOBSERVED", "inventories": []}
    for s in views:
        n = next((n for n in s["nodes"] if n["node_ref"] == node), None)
        if n is None:
            continue
        sample = (n.get("scheduler_state") if backend == "slurm" else n.get("ready")) or {}
        blockers = n.get("scheduling_blockers") or []
        if sample.get("status") != "ok":
            status = "UNKNOWN"
        elif backend != "slurm" and sample.get("value") is not True:
            status = "NOT_READY"
        else:
            status = "BLOCKED" if blockers else "READY"
        return {
            "status": status,
            "cluster_ref": s["cluster_ref"],
            "snapshot_status": s["status"],
            "collected_at": s["collected_at"],
            "observed_at": sample.get("observed_at"),
            "sample_status": sample.get("status"),
            "blockers": blockers,
        }
    return {
        "status": "NODE_MISSING",
        "inventories": [
            {
                "cluster_ref": s["cluster_ref"],
                "status": s["status"],
                "collected_at": s["collected_at"],
            }
            for s in views
        ],
    }


RECENT_RUNS = 10


def _recent_runs(service, conn, project, workload_ref):
    """Latest native jobs of this workload through the console job projection.

    JCT is the recorded outcome when one exists, else native scheduler submit to
    backend finish for a succeeded job; anything else stays unknown with a reason.
    """
    from sqlalchemy import select

    from .console import job_view
    from .store import jobs

    rows = conn.execute(
        select(jobs)
        .where(
            jobs.c.project == project,
            jobs.c.body["request"]["workload_ref"].as_string() == workload_ref,
        )
        .order_by(jobs.c.body["created_at"].as_string().desc(), jobs.c.id.desc())
        .limit(RECENT_RUNS)
    ).mappings()
    runs = []
    for row in rows:
        view, body = job_view(service, conn, row), row["body"]
        outcome = service.store.get(conn, "jct_outcome", body["attempt_id"])
        if outcome and outcome["project"] == project:
            jct = {**outcome["body"]["actual"], "source": "jct_outcome"}
        else:
            submitted, finished = (
                body.get("scheduler_submitted_at"),
                body.get("backend_finished_at"),
            )
            jct = {
                "status": "NOT_COMPLETED",
                "jct_seconds": None,
                "submitted_at": submitted,
                "finished_at": finished,
                "boundary": "native scheduler_submitted_at → backend_finished_at",
                "source": "native_timestamps",
            }
            if row["state"] == "SUCCEEDED":
                if submitted and finished:
                    seconds = (
                        datetime.fromisoformat(finished) - datetime.fromisoformat(submitted)
                    ).total_seconds()
                    ok = seconds >= 0
                    jct.update(
                        status="OBSERVED" if ok else "TIMESTAMPS_INVALID",
                        jct_seconds=seconds if ok else None,
                    )
                else:
                    jct["status"] = "TIMESTAMPS_MISSING"
        result = view["result"]
        runs.append(
            {
                "job_id": view["job_id"],
                "state": view["state"],
                "error": view["error"],
                "created_at": view["created_at"],
                "candidate_ref": view["candidate_ref"],
                "node_ref": view["node_ref"],
                "device_class": view["device_class"],
                "accelerator_model": view["accelerator_model"],
                "result": None
                if result is None
                else {
                    "outcome": result.get("outcome"),
                    "measured": result.get("measured"),
                    "evidence_kind": result.get("evidence_kind"),
                    "compute_seconds": (result.get("measurements") or {}).get("elapsed_seconds"),
                },
                "quality_passed": view["quality_passed"],
                "tracking": view["tracking"],
                "jct": jct,
            }
        )
    return runs


def _matrix(specs, rows, selected):
    """Accelerator family/model against this workload and other registered workloads."""
    sig = {s.ref: signature(s.identity) for s in specs}
    keys = sorted(
        {
            (c["device_class"], c.get("model"))
            for refs in rows.values()
            for c in refs
            if c.get("device_class") in {"gpu", "npu"}
        },
        key=lambda k: (k[0], k[1] or ""),
    )
    matrix = []
    for device_class, model in keys:

        def match(c, device_class=device_class, model=model):
            return c.get("device_class") == device_class and c.get("model") == model

        own = [c for c in rows[selected.ref] if match(c)]
        matrix.append(
            {
                "device_class": device_class,
                "model": model,
                "this_workload": {
                    "registered": len(own),
                    "submission_compatible": sum(1 for c in own if c["submittable_now"]),
                },
                "other_workloads": [
                    {
                        "workload_ref": s.ref,
                        "relation": "SAME_WORKLOAD_IDENTITY"
                        if sig[s.ref] == sig[selected.ref]
                        else "DIFFERENT_WORKLOAD_IDENTITY",
                        "registered": sum(1 for c in rows[s.ref] if match(c)),
                    }
                    for s in specs
                    if s.ref != selected.ref and any(match(c) for c in rows[s.ref])
                ],
            }
        )
    return matrix


def jct_report(service, project, workload_ref=None):
    """Workload index, and for one workload its candidates, evidence and comparisons."""
    from .console import candidate_rows
    from .inventory import latest_inventory_views
    from .service import NotFound

    store, at = service.store, now()
    with store.transaction() as conn:
        specs = [
            WorkloadSpec.model_validate(x["body"]) for x in store.list(conn, "workload", project)
        ]
        rows = {s.ref: candidate_rows(service, conn, project, s) for s in specs}
        catalog = set(service.console_workloads)
        index = [_summary(s, rows[s.ref], catalog) for s in specs]
        result = {
            "project_ref": project,
            "generated_at": at.isoformat(),
            "semantics": (
                "Counts are registered candidates and submission-condition checks, not free "
                "capacity or live device health. Evidence counts are recorded rows; missing "
                "evidence stays unknown. Only comparisons of the same workload are shown."
            ),
            "workloads": index,
            "selected": None,
        }
        if workload_ref is None:
            return result
        spec = next((s for s in specs if s.ref == workload_ref), None)
        if spec is None:
            raise NotFound("workload not found")
        policies = {arm: [] for arm in ARMS}
        for x in store.list(conn, "scheduling_profile", project):
            rec = x["body"]["policy"].get("recommendation")
            policies["static" if rec is None else rec["policy"]].append(x["ref"])
        queue_age = min(
            (
                x["body"]["policy"]["recommendation"].get(
                    "max_queue_age_seconds", DEFAULT_QUEUE_AGE
                )
                for x in store.list(conn, "scheduling_profile", project)
                if x["body"]["policy"].get("recommendation")
            ),
            default=DEFAULT_QUEUE_AGE,
        )
        inventory = latest_inventory_views(conn, project, at=at)
        candidates = [
            {
                **c,
                "readiness": _readiness(store, conn, project, spec, c, at, queue_age)
                if c.get("node_ref")
                else None,
                "node_observation": _node_observation(inventory, c)
                if c.get("node_ref")
                else {"status": "REGISTRY_ENTRY_MISSING"},
            }
            for c in rows[spec.ref]
        ]
        recent = _recent_runs(service, conn, project, spec.ref)
        matrix = _matrix(specs, rows, spec)
        summary = next(w for w in index if w["workload_ref"] == spec.ref)
        own_classes, own_models = set(summary["device_classes"]), set(summary["accelerator_models"])
        alternatives = [
            {
                **w,
                "relation": "SAME_WORKLOAD_IDENTITY"
                if w["identity_signature"] == summary["identity_signature"]
                else "DIFFERENT_WORKLOAD_IDENTITY",
            }
            for w in index
            if w["workload_ref"] != spec.ref
            and (
                set(w["device_classes"]) - own_classes or set(w["accelerator_models"]) - own_models
            )
        ]
    result["selected"] = {
        "workload": summary,
        "candidate_counts": {
            "registered": len(candidates),
            "contract_compatible": sum(1 for c in candidates if c["contract_compatible_now"]),
            "submission_compatible": summary["submission_compatible"],
        },
        "candidates": candidates,
        "policy_profiles": policies,
        "comparisons": list_comparisons(service, project, spec.ref)["items"],
        "alternatives": alternatives,
        "accelerator_matrix": matrix,
        "recent_runs": recent,
    }
    return result
