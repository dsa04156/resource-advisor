"""Evidence-based allocation ledger. Missing values never mean zero consumption."""

import math
from datetime import UTC, datetime


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
    # The scheduler's definitive refusal proves no native job was created.
    native_rejected = bool(body.get("native_rejection")) and not body.get("external_id")
    never_submitted = never_submitted or native_rejected
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
            "native_rejected": native_rejected,
            "resource_key": body.get("capability", {}).get("resource_key"),
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


def utc_instant(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.tzinfo is None:
        raise ValueError("reporting boundaries require an explicit UTC offset")
    return stamp.astimezone(UTC)


def month_period(month):
    year, index = (int(part) for part in month.split("-"))
    start = datetime(year, index, 1, tzinfo=UTC)
    return start, datetime(year + index // 12, index % 12 + 1, 1, tzinfo=UTC)


def _aware(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo is not None else None


def period_report(records, start, end, open_jobs=()):
    """Clip observed reservation intervals to [start, end) in UTC.

    Totals come only from the finalized terminal ledger. Attempts are attributed
    by their observed reservation interval; when it is unknown, the terminal
    observation time decides membership and the attempt is counted as unknown,
    never as zero. Non-terminal attempts created before the period end are listed
    separately as not finalized. Groups never mix clusters or unit kinds.
    """
    start, end = utc_instant(start), utc_instant(end)
    if end <= start:
        raise ValueError("reporting period end must follow its start")
    pending = {}
    for job in open_jobs:
        body = job["body"]
        created = _aware(body.get("created_at"))
        if created is not None and created >= end:
            continue
        key = (
            body.get("backend_cluster_id"),
            body["candidate"]["backend"],
            body["variant"]["device_class"],
            body["candidate"]["context"]["allocation_mode"],
            job["state"],
            not body.get("external_id"),
        )
        pending[key] = pending.get(key, 0) + 1
    groups, unattributable = {}, 0
    for record in records:
        body = record["body"]
        if body.get("schema_version") != "v2":
            unattributable += 1
            continue
        begin, finish = _aware(body.get("started_at")), _aware(body.get("backend_finished_at"))
        known = begin is not None and finish is not None and finish >= begin
        if known:
            overlap = (min(finish, end) - max(begin, start)).total_seconds()
            if overlap < 0 or (overlap == 0 and not (start <= begin < end)):
                continue
        else:
            marker = _aware(body.get("terminal_observed_at"))
            if marker is None:
                unattributable += 1
                continue
            if not start <= marker < end:
                continue
        key = (
            body.get("backend_cluster_id"),
            record["backend"],
            record["device_class"],
            record["allocation_mode"],
            body.get("accelerator_model"),
            body.get("resource_key"),
        )
        group = groups.setdefault(
            key,
            {
                "backend_cluster_id": key[0],
                "backend": key[1],
                "device_class": key[2],
                "allocation_mode": key[3],
                "accelerator_model": key[4],
                "resource_key": key[5],
                "attempts": 0,
                "projects": {},
                "outcomes": {},
                "clipped_attempts": 0,
                "known_allocated_device_seconds": 0.0,
                "known_allocated_cpu_seconds": 0.0,
                "unknown_allocation_attempts": 0,
                "unknown_cpu_attempts": 0,
                "whole_second_resolution_attempts": 0,
                "no_native_job_attempts": 0,
            },
        )
        group["attempts"] += 1
        group["projects"][record["project"]] = group["projects"].get(record["project"], 0) + 1
        outcome = body.get("outcome", "LEGACY_UNKNOWN")
        group["outcomes"][outcome] = group["outcomes"].get(outcome, 0) + 1
        if body.get("never_submitted"):
            group["no_native_job_attempts"] += 1
            continue
        if "WHOLE_SECOND_RESOLUTION" in body.get("uncertainty", []):
            group["whole_second_resolution_attempts"] += 1
        allocation = body.get("observed_allocation") or {}
        count, cpu = number(allocation.get("accelerator_count")), number(allocation.get("cpu"))
        if not known:
            group["unknown_allocation_attempts"] += 1
            group["unknown_cpu_attempts"] += 1
            continue
        group["clipped_attempts"] += int(begin < start or finish > end)
        if count is None:
            group["unknown_allocation_attempts"] += 1
        else:
            group["known_allocated_device_seconds"] += overlap * count
        if cpu is None:
            group["unknown_cpu_attempts"] += 1
        else:
            group["known_allocated_cpu_seconds"] += overlap * cpu
    return {
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "boundary": "[start, end) UTC",
        },
        "groups": list(groups.values()),
        "unattributable_attempts": unattributable,
        "not_finalized": [
            {
                "backend_cluster_id": key[0],
                "backend": key[1],
                "device_class": key[2],
                "allocation_mode": key[3],
                "state": key[4],
                "native_identity_unknown": key[5],
                "attempts": count,
            }
            for key, count in pending.items()
        ],
        "semantics": (
            "Finalized terminal attempts only; not_finalized attempts (running, queued, "
            "unresolved submissions) are excluded from totals and may still be charged. "
            "Observed reservation seconds clipped to the period; not utilization. "
            "Unknown intervals or counts are counted, never zero. Groups do not combine "
            "clusters, device classes, allocation modes, accelerator models or resource keys."
        ),
    }
