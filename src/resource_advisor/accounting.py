"""Evidence-based allocation ledger. Missing values never mean zero consumption."""

import math
from datetime import datetime


def interval(start, end):
    if not start or not end:
        return None
    try:
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if a.tzinfo is None or b.tzinfo is None or b < a:
            return None
        return (b - a).total_seconds()
    except (ValueError, TypeError):
        return None


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError):
        return None


def ledger_record(row, state, body, result=None):
    context = body["candidate"]["context"]
    allocation = body.get("allocation") or {}
    never_submitted = bool(body.get("cancel_before_submit") and not body.get("external_id")) or (
        row["state"] == "VALIDATED" and not body.get("external_id")
    )
    start, end = body.get("started_at"), body.get("backend_finished_at")
    duration = interval(start, end)
    count = number(allocation.get("accelerator_count"))
    allocated = duration * count if duration is not None and count is not None else None
    if never_submitted:
        allocated = 0.0
    valid = result is not None and state != "RESULT_INVALID"
    measurements = result.get("measurements") if valid else None
    queued = body.get("scheduler_submitted_at") or body.get("queued_at")
    queue_seconds = interval(queued, start)
    queue_whole_seconds = queue_seconds is not None and all(
        "." not in stamp for stamp in (queued, start)
    )
    return {
        "attempt_id": body["attempt_id"],
        "project": row["project"],
        "backend": body["candidate"]["backend"],
        "device_class": body["variant"]["device_class"],
        "allocation_mode": context["allocation_mode"],
        "allocated_device_seconds": allocated,
        "measured_compute_seconds": measurements.get("elapsed_seconds") if measurements else None,
        "queue_seconds": queue_seconds,
        "body": {
            "schema_version": "v2",
            "accelerator_model": context["accelerator_model"],
            "backend_cluster_id": body.get("backend_cluster_id"),
            "job_id": row["id"],
            "outcome": str(state),
            "error": body.get("error"),
            "result_valid": valid,
            "requested_resources": context["resources"],
            "observed_allocation": allocation or None,
            "termination_receipt_ref": body["attempt_id"] if body.get("termination") else None,
            "never_submitted": never_submitted,
            "allocation_interval_seconds": duration,
            "started_at": start,
            "backend_finished_at": end,
            "terminal_observed_at": body.get("finished_at"),
            "cancel_requested_at": body.get("cancel_requested_at"),
            "cancel_dispatch_started_at": body.get("cancel_dispatch_started_at"),
            "cancel_acknowledged_at": body.get("cancel_acknowledged_at"),
            "cancel_confirmed_at": body.get("finished_at") if state == "CANCELED" else None,
            "cancel_dispatch_wait_seconds": interval(
                body.get("cancel_requested_at"), body.get("cancel_dispatch_started_at")
            ),
            "cancel_confirmation_seconds": interval(
                body.get("cancel_requested_at"), body.get("finished_at")
            )
            if state == "CANCELED"
            else None,
            "submitted_at": queued,
            "submission_time_source": "scheduler"
            if body.get("scheduler_submitted_at")
            else "submit_response",
            "collection_seconds": interval(body.get("collecting_since"), body.get("finished_at")),
            "preparation_seconds": interval(start, body.get("execution_started_at")),
            "container_runtime_seconds": interval(body.get("execution_started_at"), end),
            "allocated_cpu_seconds": duration * number(allocation["cpu"])
            if duration is not None and number(allocation.get("cpu")) is not None
            else (0.0 if never_submitted else None),
            "allocation_memory_mib": number(allocation.get("memory_mib")),
            "allocation_scope": allocation.get("source"),
            "uncertainty": []
            if never_submitted
            else [
                reason
                for reason, missing in [
                    ("ALLOCATION_INTERVAL_UNKNOWN", duration is None),
                    ("ACCELERATOR_ALLOCATION_UNKNOWN", count is None),
                    ("QUEUE_INTERVAL_UNKNOWN", queue_seconds is None),
                    ("WHOLE_SECOND_RESOLUTION", duration == 0),
                    ("QUEUE_WHOLE_SECOND_RESOLUTION", queue_whole_seconds),
                ]
                if missing
            ],
            "semantics": "allocated reservations, not utilization; unknowns are null",
        },
    }


def summarize(records):
    groups = {}
    for record in records:
        key = (
            record["backend"],
            record["device_class"],
            record["allocation_mode"],
            record["body"].get("accelerator_model"),
            record["body"].get("backend_cluster_id"),
        )
        group = groups.setdefault(
            key,
            {
                "backend": key[0],
                "device_class": key[1],
                "allocation_mode": key[2],
                "accelerator_model": key[3],
                "backend_cluster_id": key[4],
                "attempts": 0,
                "outcomes": {},
                "known_allocated_device_seconds": 0.0,
                "unknown_allocation_attempts": 0,
                "legacy_unqualified_attempts": 0,
            },
        )
        group["attempts"] += 1
        body = record["body"]
        outcome = body.get("outcome", "LEGACY_UNKNOWN")
        group["outcomes"][outcome] = group["outcomes"].get(outcome, 0) + 1
        legacy = body.get("schema_version") != "v2"
        value = None if legacy else record["allocated_device_seconds"]
        group["legacy_unqualified_attempts"] += int(legacy)
        if value is None:
            group["unknown_allocation_attempts"] += 1
        else:
            group["known_allocated_device_seconds"] += value
    return list(groups.values())
