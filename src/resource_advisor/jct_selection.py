"""Pure, experimental finish-time selection; native admission remains authoritative.

Inputs must be frozen before choosing a candidate, in one common clock domain.
No collector, Resource Advisor registration, or production selection is invoked.
Use ``dataclasses.asdict`` to archive inputs and the decision before submission.
Missing evidence abstains (``candidate_ref=None``), so any fallback must be an
explicit, separately archived caller decision. This is an expected FIFO finish
estimate for bounded equal-priority work, never a native admission guarantee.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from heapq import heapify, heappop, heappush
from math import isfinite
from typing import Literal

PROFILE_QUEUE = "profile_queue"
ROUND_ROBIN = "round_robin"
V2_PROFILE_ONLY = "profile_only"


@dataclass(frozen=True)
class CandidateIdentity:
    """Exact workload/runtime and native resource route; names alone are insufficient."""

    candidate_ref: str
    backend: str
    cluster_ref: str
    node_ref: str
    resource_key: str
    runtime_digest: str
    workload_digest: str


@dataclass(frozen=True)
class MeasuredProfile:
    """Receipt-qualified frozen costs from nonoverlapping observed boundaries.

    Preparation spans native allocation start to compute start; release spans
    compute finish to native completion. Idle admission spans native acceptance to
    allocation start. Readmission is the prior occupant's native completion to the
    next allocation start, replacing idle admission when a slot must be waited for.
    V2 service is its frozen allocation-duration estimate, not a sum adding startup
    twice. Unknown costs stay None. Quantized point estimates retain their raw
    evidence, timing uncertainty and explanatory notes in the caller's archive.
    """

    identity: CandidateIdentity
    evidence_ref: str
    measured_at: float
    compute_seconds: float | None
    preparation_seconds: float | None
    admission_seconds: float | None
    release_seconds: float | None
    v2_service_seconds: float | None
    qualified: bool = False
    quality_passed: bool = False
    synthetic: bool = False
    readmission_seconds: float | None = None
    timing_uncertainty_seconds: float | None = None
    timing_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkItem:
    """Occupancy is allocation start to native completion, excluding admission.

    Running work needs observed allocation start; pending work needs its own
    measured expected admission gap. Preserve pending items in observed FIFO order.
    """

    job_id: str
    expected_occupancy_seconds: float | None
    started_at: float | None = None
    expected_admission_seconds: float | None = None


@dataclass(frozen=True)
class QueueSnapshot:
    """Complete route-scoped demand, including external contention if qualified.

    Aggregate counts cannot assert completeness. Logical sharing slots are distinct
    from physical devices; concurrency-specific profile qualification belongs to
    the collector. observed_job_ids may include terminal jobs to reconcile intents.
    """

    identity: CandidateIdentity
    snapshot_ref: str
    observed_at: float
    capacity: int
    complete: bool = False
    running: tuple[WorkItem, ...] = ()
    pending: tuple[WorkItem, ...] = ()
    capacity_unit: Literal["physical_device", "logical_slot"] = "physical_device"
    observed_job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubmitIntent:
    """Unresolved local demand, retained through uncertain native acceptance."""

    identity: CandidateIdentity
    job_id: str
    submitted_at: float
    expected_occupancy_seconds: float | None
    expected_admission_seconds: float | None


@dataclass(frozen=True)
class CandidatePrediction:
    candidate_ref: str
    queue_wait_seconds: float | None
    predicted_jct_seconds: float | None
    reasons: tuple[str, ...] = ()
    compute_seconds: float | None = None
    preparation_seconds: float | None = None
    admission_seconds: float | None = None
    release_seconds: float | None = None
    profile_evidence_ref: str | None = None
    queue_snapshot_ref: str | None = None
    accounted_job_ids: tuple[str, ...] = ()
    capacity: int | None = None
    capacity_unit: str | None = None
    deduplicated_intent_ids: tuple[str, ...] = ()
    v2_service_seconds: float | None = None
    timing_uncertainty_seconds: float | None = None
    timing_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectionDecision:
    """Archive alongside inputs before the selected job's outcome is known."""

    policy: str
    candidate_ref: str | None
    predictions: tuple[CandidatePrediction, ...]


def _profile_reason(profile, candidate, now_seconds, max_age, policy):
    if profile.identity != candidate:
        return "PROFILE_IDENTITY_MISMATCH"
    if not profile.qualified:
        return "PROFILE_UNQUALIFIED"
    if not profile.quality_passed:
        return "PROFILE_QUALITY_FAILED"
    if profile.synthetic:
        return "PROFILE_SYNTHETIC"
    if not isfinite(profile.measured_at):
        return "PROFILE_TIME_INVALID"
    if profile.measured_at > now_seconds:
        return "PROFILE_FROM_FUTURE"
    if now_seconds - profile.measured_at > max_age:
        return "PROFILE_STALE"
    components = (
        (profile.v2_service_seconds,)
        if policy == V2_PROFILE_ONLY
        else (
            profile.compute_seconds,
            profile.preparation_seconds,
            profile.admission_seconds,
            profile.release_seconds,
        )
    )
    if any(value is None for value in components):
        return "PROFILE_COMPONENT_UNKNOWN"
    if any(not isfinite(value) or value < 0 for value in components):
        return "PROFILE_COMPONENT_INVALID"
    return None


def _queue_reason(queue, now_seconds):
    if type(queue.capacity) is not int or queue.capacity < 1:
        return "QUEUE_CAPACITY_INVALID"
    job_ids = [job.job_id for job in (*queue.running, *queue.pending)]
    if len(set(job_ids)) != len(job_ids) or any(not job_id for job_id in job_ids):
        return "QUEUE_JOB_ID_AMBIGUOUS"
    if len(queue.running) > queue.capacity:
        return "QUEUE_CAPACITY_INVALID"
    for job in (*queue.running, *queue.pending):
        if job.expected_occupancy_seconds is None:
            return "QUEUE_WORK_UNKNOWN"
        if not isfinite(job.expected_occupancy_seconds) or job.expected_occupancy_seconds < 0:
            return "QUEUE_WORK_INVALID"
    for job in queue.running:
        if job.started_at is None:
            return "RUNNING_START_UNKNOWN"
        if not isfinite(job.started_at) or job.started_at > queue.observed_at:
            return "RUNNING_START_INVALID"
        if now_seconds - job.started_at >= job.expected_occupancy_seconds:
            return "RUNNING_EXPECTATION_EXCEEDED"
    for job in queue.pending:
        if job.expected_admission_seconds is None:
            return "QUEUE_ADMISSION_UNKNOWN"
        if not isfinite(job.expected_admission_seconds) or job.expected_admission_seconds < 0:
            return "QUEUE_ADMISSION_INVALID"
    return None


def select_candidate(
    policy: Literal["round_robin", "profile_only", "profile_queue"],
    candidates: Sequence[CandidateIdentity],
    profiles: Sequence[MeasuredProfile],
    *,
    now_seconds: float,
    queues: Sequence[QueueSnapshot] = (),
    max_queue_age_seconds: float = 5.0,
    max_profile_age_seconds: float = 86400.0,
    intents: Sequence[SubmitIntent] = (),
    rr_index: int = 0,
    cohort_backlog_seconds: Mapping[str, float] | None = None,
) -> SelectionDecision:
    """Select deterministically; controls never consume live queue/intents.

    Round Robin cycles in caller-supplied frozen pool order. V2 Profile Only adds
    its service estimate to static cohort backlog without elapsed-time decay. None
    initializes a new cohort with known zero backlog; supplied mappings must name
    every candidate. Profile + Queue uses fresh complete observations and reconciled
    intents. Prediction ties use candidate reference, independent of input ordering.
    """
    if policy not in {ROUND_ROBIN, V2_PROFILE_ONLY, PROFILE_QUEUE}:
        raise ValueError("unknown selection policy")
    if not isfinite(now_seconds) or any(
        not isfinite(age) or age < 0 for age in (max_queue_age_seconds, max_profile_age_seconds)
    ):
        raise ValueError("selection time and freshness limits must be finite and valid")
    if len({candidate.candidate_ref for candidate in candidates}) != len(candidates):
        raise ValueError("candidate references must be unique")
    if policy == ROUND_ROBIN:
        predictions = tuple(
            CandidatePrediction(c.candidate_ref, None, None, ("ROUND_ROBIN_POOL_ORDER",))
            for c in candidates
        )
        selected = candidates[rr_index % len(candidates)].candidate_ref if candidates else None
        return SelectionDecision(policy, selected, predictions)
    predictions = []
    for candidate in candidates:
        matches = [p for p in profiles if p.identity.candidate_ref == candidate.candidate_ref]
        profile = matches[0] if matches else None
        reason = (
            "PROFILE_AMBIGUOUS"
            if len(matches) > 1
            else _profile_reason(profile, candidate, now_seconds, max_profile_age_seconds, policy)
            if profile
            else "PROFILE_MISSING"
        )
        if reason:
            predictions.append(CandidatePrediction(candidate.candidate_ref, None, None, (reason,)))
            continue
        if policy == V2_PROFILE_ONLY:
            wait = (
                cohort_backlog_seconds.get(candidate.candidate_ref)
                if cohort_backlog_seconds is not None
                else 0.0
            )
            if wait is None or not isfinite(wait) or wait < 0:
                predictions.append(
                    CandidatePrediction(
                        candidate.candidate_ref, None, None, ("V2_BACKLOG_UNKNOWN_OR_INVALID",)
                    )
                )
            else:
                predictions.append(
                    CandidatePrediction(
                        candidate.candidate_ref,
                        wait,
                        wait + profile.v2_service_seconds,
                        ("V2_STATIC_COHORT_BACKLOG",),
                        profile_evidence_ref=profile.evidence_ref,
                        v2_service_seconds=profile.v2_service_seconds,
                    )
                )
            continue
        matches = [q for q in queues if q.identity.candidate_ref == candidate.candidate_ref]
        queue = matches[0] if matches else None
        reason = None
        if queue is None:
            reason = "QUEUE_MISSING"
        elif len(matches) > 1:
            reason = "QUEUE_AMBIGUOUS"
        elif queue.identity != candidate:
            reason = "QUEUE_IDENTITY_MISMATCH"
        elif not queue.complete:
            reason = "QUEUE_INCOMPLETE"
        elif not isfinite(queue.observed_at):
            reason = "QUEUE_TIME_INVALID"
        elif queue.observed_at > now_seconds:
            reason = "QUEUE_FROM_FUTURE"
        elif now_seconds - queue.observed_at > max_queue_age_seconds:
            reason = "QUEUE_STALE"
        else:
            reason = _queue_reason(queue, now_seconds)
        if reason:
            predictions.append(CandidatePrediction(candidate.candidate_ref, None, None, (reason,)))
            continue
        observed_ids = {job.job_id for job in (*queue.running, *queue.pending)}
        observed_ids.update(queue.observed_job_ids)
        deduplicated = []
        pending = list(queue.pending)
        for intent in sorted(intents, key=lambda i: (i.submitted_at, i.job_id)):
            if intent.identity.candidate_ref != candidate.candidate_ref:
                continue
            if intent.identity != candidate:
                reason = "INTENT_IDENTITY_MISMATCH"
                break
            if intent.job_id in observed_ids:
                deduplicated.append(intent.job_id)
                continue
            if not isfinite(intent.submitted_at) or intent.submitted_at > now_seconds:
                reason = "INTENT_TIME_INVALID"
                break
            observed_ids.add(intent.job_id)
            pending.append(
                WorkItem(
                    intent.job_id,
                    intent.expected_occupancy_seconds,
                    expected_admission_seconds=intent.expected_admission_seconds,
                )
            )
        queue = replace(queue, pending=tuple(pending))
        reason = reason or _queue_reason(queue, now_seconds)
        if reason:
            predictions.append(CandidatePrediction(candidate.candidate_ref, None, None, (reason,)))
            continue
        slots = [
            max(0.0, job.expected_occupancy_seconds - (now_seconds - job.started_at))
            for job in queue.running
        ]
        slots.extend([0.0] * (queue.capacity - len(slots)))
        heapify(slots)
        for job in queue.pending:
            available = heappop(slots)
            heappush(
                slots, available + job.expected_admission_seconds + job.expected_occupancy_seconds
            )
        wait = min(slots)
        admission = profile.readmission_seconds if wait > 0 else profile.admission_seconds
        if admission is None or not isfinite(admission) or admission < 0:
            predictions.append(
                CandidatePrediction(
                    candidate.candidate_ref, None, None, ("READMISSION_UNKNOWN_OR_INVALID",)
                )
            )
            continue
        total = (
            wait
            + profile.compute_seconds
            + profile.preparation_seconds
            + admission
            + profile.release_seconds
        )
        predictions.append(
            CandidatePrediction(
                candidate.candidate_ref,
                wait,
                total,
                compute_seconds=profile.compute_seconds,
                preparation_seconds=profile.preparation_seconds,
                admission_seconds=admission,
                release_seconds=profile.release_seconds,
                profile_evidence_ref=profile.evidence_ref,
                queue_snapshot_ref=queue.snapshot_ref,
                accounted_job_ids=tuple(job.job_id for job in (*queue.running, *queue.pending)),
                capacity=queue.capacity,
                capacity_unit=queue.capacity_unit,
                deduplicated_intent_ids=tuple(deduplicated),
                timing_uncertainty_seconds=profile.timing_uncertainty_seconds,
                timing_notes=profile.timing_notes,
            )
        )
    ranked = [p for p in predictions if p.predicted_jct_seconds is not None]
    selected = (
        min(ranked, key=lambda p: (p.predicted_jct_seconds, p.candidate_ref)) if ranked else None
    )
    return SelectionDecision(
        policy, selected.candidate_ref if selected else None, tuple(predictions)
    )
