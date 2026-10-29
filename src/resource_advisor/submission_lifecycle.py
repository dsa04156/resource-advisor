"""Lifecycle diagnostics and audited resolution of uncertain native submissions."""

import re

from sqlalchemy import select

from .contracts import TERMINAL, State
from .store import usage

# Two successful queries without a match, separated by worker cycles, before an
# operator may acknowledge the attempt. Failed or ambiguous queries reset the count.
# Acknowledgement never proves absence and never ends reconciliation.
MIN_NO_MATCH_CHECKS = 2


def unresolved_submission(row):
    body = row["body"]
    return not body.get("external_id") and (
        row["state"] == State.SUBMISSION_UNKNOWN
        or (row["state"] == State.CANCEL_REQUESTED and not body.get("cancel_before_submit"))
    )


def resolution_blockers(row):
    record = row["body"].get("submission_reconciliation") or {}
    blockers = [] if unresolved_submission(row) else ["NOT_AN_UNRESOLVED_SUBMISSION"]
    if (
        record.get("last_outcome") != "NO_MATCH"
        or record.get("consecutive_no_match", 0) < MIN_NO_MATCH_CHECKS
    ):
        blockers.append("INSUFFICIENT_NO_MATCH_EVIDENCE")
    return blockers


SLURM_WAIT = [
    (r"(?:Assoc|QOS)(?:Grp|Max)\w*", "QUOTA_OR_LIMIT_PENDING"),
    (r"Resources", "CAPACITY_PENDING"),
    (r"Priority", "PRIORITY_PENDING"),
    (r"(?:Dependency|BeginTime|JobHeldUser)", "REQUESTER_CONSTRAINT"),
    (
        r"(?:ReqNodeNotAvail\w*|PartitionDown|PartitionInactive|NodeDown|JobHeldAdmin)",
        "NODE_OR_ADMIN_UNAVAILABLE",
    ),
]


def classify_wait(row):
    """Map the persisted native observation to a stable category; keep the raw reason.

    Pending on quota is not a rejection. Unrecognized reasons stay UNCLASSIFIED.
    """
    body = row["body"]
    backend = body["candidate"]["backend"]
    reason = body.get("scheduler_reason")
    if body.get("native_rejection"):
        category = "NATIVE_REJECTED"
    elif row["state"] in TERMINAL:
        category = "TERMINAL"
    elif row["state"] in {State.RUNNING, State.COLLECTING}:
        category = "NOT_WAITING"
    elif not body.get("external_id"):
        category = "NO_NATIVE_IDENTITY"
    elif backend == "slurm":
        category = next(
            (c for pattern, c in SLURM_WAIT if reason and re.fullmatch(pattern, reason)),
            "UNCLASSIFIED" if reason else "UNKNOWN",
        )
    else:
        category = kueue_wait(body.get("admission"), reason)
    return {"backend": backend, "category": category, "native_reason": reason}


def kueue_wait(admission, reason):
    if not admission or admission.get("error"):
        return "ADMISSION_UNOBSERVED" if reason == "AdmissionPending" else "UNKNOWN"
    conditions = {c.get("type"): c for c in admission.get("conditions") or []}
    for kind in ("Evicted", "Preempted"):
        if conditions.get(kind, {}).get("status") == "True":
            return "EVICTED_OR_PREEMPTED"
    if conditions.get("Admitted", {}).get("status") == "True":
        return "CAPACITY_PENDING" if reason == "Unschedulable" else "ADMITTED_STARTING"
    quota = conditions.get("QuotaReserved", {})
    if quota.get("status") == "False" and "insufficient" in str(quota.get("message", "")):
        return "QUOTA_OR_LIMIT_PENDING"
    return "ADMISSION_PENDING" if quota else "UNKNOWN"


def lifecycle(service, conn, row):
    body = row["body"]
    record = (
        conn.execute(select(usage).where(usage.c.attempt_id == body["attempt_id"]))
        .mappings()
        .first()
    )
    blockers = resolution_blockers(row)
    return {
        **service.public_job(row),
        "backend": body["candidate"]["backend"],
        "backend_cluster_id": body.get("backend_cluster_id"),
        "native_identity": {
            "external_id": body.get("external_id"),
            "backend_uid": body.get("backend_uid"),
            "placement": body.get("placement"),
        },
        "unresolved_submission": unresolved_submission(row),
        "submission_reconciliation": body.get("submission_reconciliation"),
        "submission_resolution": body.get("submission_resolution"),
        "native_rejection": body.get("native_rejection"),
        "wait": classify_wait(row),
        "admission": body.get("admission"),
        "lifecycle_events": body.get("lifecycle_events", []),
        "usage": None
        if record is None
        else {
            "allocated_device_seconds": record["allocated_device_seconds"],
            "uncertainty": record["body"].get("uncertainty"),
            "never_submitted": record["body"].get("never_submitted"),
        },
        "resolution": {
            "eligible": not blockers,
            "blockers": blockers,
            "minimum_no_match_checks": MIN_NO_MATCH_CHECKS,
            "decisions": ["ACKNOWLEDGE_UNRESOLVED"],
        },
    }
