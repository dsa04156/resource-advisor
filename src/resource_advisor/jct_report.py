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


def jct_report(service, project, workload_ref=None):
    """Workload index, and for one workload its candidates, evidence and comparisons."""
    from .console import candidate_rows
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
        candidates = [
            {**c, "readiness": _readiness(store, conn, project, spec, c, at, queue_age)}
            if c.get("node_ref")
            else {**c, "readiness": None}
            for c in rows[spec.ref]
        ]
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
    }
    return result
