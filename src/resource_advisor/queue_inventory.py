"""Kueue read models. Reservation/admission are never physical utilization."""

from .inventory import quantity


def conditions(obj):
    generation = obj.get("metadata", {}).get("generation")
    return [
        {
            **{k: c.get(k) for k in ("type", "status", "reason", "message", "lastTransitionTime")},
            "current_generation": generation is None
            or c.get("observedGeneration", generation) == generation,
        }
        for c in obj.get("status", {}).get("conditions", [])
    ]


def condition_value(items, kind):
    condition = next((c for c in items if c["type"] == kind), None)
    if not condition or not condition["current_generation"]:
        return None
    return {"True": True, "False": False}.get(condition["status"])


def amount(value):
    try:
        return float(quantity(value)) if value is not None else None
    except (ValueError, TypeError):
        return None


def queue_view(obj, resource_types, *, cluster=False):
    status, spec = obj.get("status", {}), obj["spec"]
    checks = conditions(obj)
    nominal = {
        (f["name"], r["name"]): r
        for group in spec.get("resourceGroups", [])
        for f in group.get("flavors", [])
        for r in f.get("resources", [])
    }
    reservations = {
        (f["name"], r["name"]): r
        for f in status.get("flavorsReservation", [])
        for r in f.get("resources", [])
    }
    admitted = {
        (f["name"], r["name"]): r
        for f in status.get("flavorsUsage", [])
        for r in f.get("resources", [])
    }
    resources = []
    for flavor, name in sorted(nominal.keys() | reservations.keys() | admitted.keys()):
        key = (flavor, name)
        registered = resource_types.get(name)
        resources.append(
            {
                "flavor": flavor,
                "resource": name,
                "unit": "cores"
                if name == "cpu"
                else "bytes"
                if name in {"memory", "ephemeral-storage"}
                else "resource_units",
                "device_class": registered.device_class if registered else "unknown",
                "allocation_mode": registered.allocation_mode if registered else "unknown",
                "nominal_quota": amount(nominal.get(key, {}).get("nominalQuota")),
                "borrowing_limit": amount(nominal.get(key, {}).get("borrowingLimit")),
                "reserved": amount(reservations.get(key, {}).get("total")),
                "admitted": amount(admitted.get(key, {}).get("total")),
                "borrowed_reservation": amount(reservations.get(key, {}).get("borrowed")),
            }
        )
    return {
        "ref": obj["metadata"]["name"],
        "cluster_queue": obj["metadata"]["name"] if cluster else spec.get("clusterQueue"),
        "scope": "cluster_queue_all_namespaces" if cluster else "local_queue_namespace",
        "active": condition_value(checks, "Active"),
        "stop_policy": spec.get("stopPolicy"),
        "queueing_strategy": spec.get("queueingStrategy") if cluster else None,
        "cohort": spec.get("cohort") if cluster else None,
        "conditions": checks,
        "pending_workloads": status.get("pendingWorkloads"),
        "reserving_workloads": status.get("reservingWorkloads"),
        "admitted_workloads": status.get("admittedWorkloads"),
        "resources": resources,
        "semantics": "Scheduler quota/reservation/admission; not utilization, immediate availability or guaranteed queue order. Null is unknown or not declared.",
    }


def workload_view(obj):
    checks = conditions(obj)
    if condition_value(checks, "Finished") is True:
        return None
    status = obj.get("status", {})
    return {
        "ref": obj["metadata"]["name"],
        "queue": obj["spec"].get("queueName"),
        "admitted": condition_value(checks, "Admitted"),
        "quota_reserved": condition_value(checks, "QuotaReserved"),
        "created_at": obj["metadata"]["creationTimestamp"],
        "priority": obj["spec"].get("priority"),
        "priority_class": obj["spec"].get("priorityClassName"),
        "priority_source": obj["spec"].get("priorityClassSource"),
        "cluster_queue": status.get("admission", {}).get("clusterQueue"),
        "conditions": checks,
        "admission_checks": [
            {k: c.get(k) for k in ("name", "state", "message", "lastTransitionTime")}
            for c in status.get("admissionChecks", [])
        ],
    }
