"""Opt-in heterogeneous recommendation above the existing policy compiler.

Evidence is immutable, operator/collector-imported and project-scoped. Missing,
stale, synthetic or mismatched evidence never ranks a candidate: the ranker
abstains (NEEDS_PROFILE / ESTIMATE_UNAVAILABLE) unless the profile explicitly
records the legacy fallback. Kueue, kube-scheduler and Slurm keep admission and
final node choice; a pool only restricts them to operator-verified members.
Estimates are frozen point predictions with descriptive spread, not calibrated
confidence, guarantees or optimality claims.
"""

from dataclasses import asdict
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_serializer, model_validator

from .contracts import TERMINAL, Backend, CapabilitySnapshot, Contract, Digest, Ref, now, signature
from .jct_selection import (
    PHASES,
    CandidateIdentity,
    MeasuredProfile,
    QueueSnapshot,
    SubmitIntent,
    WorkItem,
    select_candidate,
)

VERSION = "hetero-rec-v1"
# v2 replaces the separately bounded staging phase with destination-measured
# NetworkProfiles instead of a topology link model; v1 records keep their meaning.
MEASURED_VERSION = "hetero-rec-v2"
PhaseName = Literal[PHASES]
FINGERPRINT = (
    "arch",
    "accelerator_vendor",
    "accelerator_model",
    "device_class",
    "allocation_mode",
    "resource_key",
    "runtime_versions",
    "environment_digest",
    "memory_model",
    "power_mode",
)


class RecommendationPolicy(Contract):
    """B3/B4/B5 policies. ``fallback=legacy`` is explicit and always recorded."""

    version: Literal["hetero-rec-v1", "hetero-rec-v2"] = VERSION
    policy: Literal["profile_only", "profile_queue", "profile_queue_network"]
    max_profile_age_seconds: int = Field(default=86400, ge=1, le=7 * 86400)
    max_queue_age_seconds: int = Field(default=30, ge=1, le=3600)
    topology_snapshot_ref: Ref | None = None
    fallback: Literal["none", "legacy"] = "none"

    @model_validator(mode="after")
    def network_needs_topology(self):
        if self.version == MEASURED_VERSION:
            if self.topology_snapshot_ref is not None:
                raise ValueError("measured-network mode uses NetworkProfiles, not a topology")
        elif (self.policy == "profile_queue_network") != (self.topology_snapshot_ref is not None):
            raise ValueError("only the network policy takes, and it requires, a topology snapshot")
        return self


class HardwareFingerprint(Contract):
    accelerator_vendor: str = Field(min_length=1, max_length=64)
    accelerator_model: str = Field(min_length=1, max_length=256)
    device_class: Literal["gpu", "npu", "cpu"]
    memory_model: Literal["discrete", "unified", "host"]
    dedicated_memory_mib: int | None = Field(default=None, ge=1)
    allocation_mode: Literal["physical_device", "virtual_slot", "cpu_only"]
    physical_device_ref: Ref | None = None
    logical_slot: bool

    @model_validator(mode="after")
    def memory_and_slots_are_not_conflated(self):
        if self.memory_model != "discrete" and self.dedicated_memory_mib is not None:
            raise ValueError("shared/host memory is not dedicated accelerator memory")
        if self.logical_slot != (self.allocation_mode == "virtual_slot"):
            raise ValueError("logical sharing slots are distinct from physical devices")
        return self


class JctProfile(Contract):
    """Receipt-derived phase timings for one exact workload/runtime/route identity."""

    schema_version: Literal["v1"] = "v1"
    ref: Ref
    project_ref: Ref
    workload_ref: Ref
    candidate_ref: Ref
    backend: Backend
    cluster_ref: Ref
    node_ref: Ref
    resource_key: str | None = None
    workload_signature: Digest
    context_signature: Digest
    environment_digest: Digest
    runtime_versions: dict[str, str]
    hardware: HardwareFingerprint
    precision: str = Field(min_length=1)
    batch_size: int = Field(ge=1)
    input_shape: tuple[int, ...] = Field(min_length=1)
    dataset_version: str = Field(min_length=1, max_length=256)
    measured_at: AwareDatetime
    valid_for_seconds: int = Field(ge=1, le=7 * 86400)
    repeats: int = Field(ge=1, le=1000)
    admission_seconds: float | None = Field(default=None, ge=0)
    readmission_seconds: float | None = Field(default=None, ge=0)
    phases: dict[PhaseName, float | None] = Field(min_length=1)
    overlap_groups: tuple[tuple[PhaseName, ...], ...] = ()
    staging_boundary: Literal["separate", "inside_compute", "not_required"]
    excluded_preexisting_phases: tuple[PhaseName, ...] = ()
    jct_stddev_seconds: float | None = Field(default=None, ge=0)
    peak_memory_mib: float | None = Field(default=None, ge=0)
    load: dict[str, float | str | None] = Field(default_factory=dict)
    quality_passed: bool
    synthetic: bool
    job_ids: tuple[Ref, ...] = Field(min_length=1, max_length=1000)
    artifact_refs: tuple[str, ...] = ()
    source: Literal["ra_jobs", "operator_import"]

    @model_validator(mode="after")
    def phases_are_not_double_counted(self):
        if any(v is not None and v < 0 for v in self.phases.values()):
            raise ValueError("phase durations must be nonnegative")
        grouped = [name for group in self.overlap_groups for name in group]
        if len(set(grouped)) != len(grouped) or any(n not in self.phases for n in grouped):
            raise ValueError("overlap groups must be disjoint declared phases")
        if any(len(group) < 2 for group in self.overlap_groups):
            raise ValueError("an overlap group needs at least two phases")
        if (self.staging_boundary == "separate") != ("data_staging" in self.phases):
            raise ValueError("only a separately bounded staging phase may be measured")
        # A span prepared before the job (warm service) must not reenter its JCT.
        if set(self.excluded_preexisting_phases) & set(self.phases):
            raise ValueError("preexisting preparation/service spans cannot also be counted")
        return self


class NodePool(Contract):
    """Operator-declared, verified-equivalent members; never inferred from labels."""

    ref: Ref
    project_ref: Ref
    backend: Backend
    cluster_ref: Ref
    member_capability_refs: tuple[Ref, ...] = Field(min_length=1, max_length=64)
    kubernetes_node_selector: dict[str, str] = Field(default_factory=dict)
    slurm_constraint: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    # Evidence that the Slurm feature selects exactly these members.
    slurm_constraint_exclusive_evidence_ref: Ref | None = None
    verification_evidence_ref: Ref

    @model_validator(mode="after")
    def unique_members(self):
        if len(set(self.member_capability_refs)) != len(self.member_capability_refs):
            raise ValueError("duplicate pool member")
        if self.backend == "kubernetes" and (
            self.slurm_constraint or self.slurm_constraint_exclusive_evidence_ref
        ):
            raise ValueError("Slurm constraint on a Kubernetes pool")
        if self.backend == "slurm" and self.kubernetes_node_selector:
            raise ValueError("Kubernetes selector on a Slurm pool")
        return self


class QueueItem(Contract):
    job_id: str = Field(min_length=1, max_length=128)
    expected_occupancy_seconds: float | None = Field(default=None, ge=0)
    started_at: AwareDatetime | None = None
    expected_admission_seconds: float | None = Field(default=None, ge=0)


class RouteQueueSnapshot(Contract):
    """Route-scoped native demand. ``complete`` only when all occupants are known."""

    ref: Ref
    project_ref: Ref
    backend: Backend
    cluster_ref: Ref
    route_ref: Ref
    resource_key: str | None = None
    observed_at: AwareDatetime
    complete: bool
    incomplete_reasons: tuple[str, ...] = ()
    capacity: int = Field(ge=1, le=4096)
    capacity_unit: Literal["physical_device", "logical_slot"] = "physical_device"
    running: tuple[QueueItem, ...] = ()
    pending: tuple[QueueItem, ...] = ()
    observed_job_ids: tuple[str, ...] = ()
    source: str = Field(min_length=1, max_length=128)
    evidence_ref: Ref

    @model_validator(mode="after")
    def incomplete_is_explained(self):
        if not self.complete and not self.incomplete_reasons:
            raise ValueError("an incomplete queue snapshot must state why")
        return self


class NodeAddress(Contract):
    backend: Backend
    cluster_ref: Ref
    node_ref: Ref


class DataLocation(Contract):
    """Where a workload's dataset lives and which nodes are permitted to read it."""

    ref: Ref
    project_ref: Ref
    workload_ref: Ref
    dataset_version: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(ge=0, le=10**15)
    source_endpoint_ref: Ref
    storage_kind: str = Field(min_length=1, max_length=64)
    accessible_nodes: tuple[NodeAddress, ...] = Field(max_length=256)
    provenance_ref: Ref
    observed_at: AwareDatetime
    # Exact object identity; measured-network mode matches NetworkProfiles on it.
    content_digest: Digest | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_signature(self, handler):
        value = handler(self)
        if self.content_digest is None:
            value.pop("content_digest", None)
        return value


def _epoch(value):
    return (
        datetime.fromisoformat(value).timestamp() if isinstance(value, str) else value.timestamp()
    )


def _latest(items):
    return max(items, key=lambda x: (x["body"]["observed_at"], x["ref"])) if items else None


def data_location(service, conn, project, spec):
    """The single declared location of this dataset version; ambiguity fails closed."""
    items = [
        x
        for x in service.store.list(conn, "data_location", project)
        if x["body"]["workload_ref"] == spec.ref
        and x["body"]["dataset_version"] == spec.identity.dataset_version
    ]
    if len(items) > 1:
        return None, "DATA_LOCATION_AMBIGUOUS"
    return (DataLocation.model_validate(items[0]["body"]), None) if items else (None, None)


def resolve_pool(service, conn, project, candidate, cap, *, at=None):
    """Pool target for one candidate or exclusion reasons; members must match exactly."""
    pools = [
        x["body"]
        for x in service.store.list(conn, "node_pool", project)
        if cap.ref in x["body"]["member_capability_refs"]
    ]
    if len(pools) != 1:
        return None, ["POOL_UNDECLARED" if not pools else "POOL_AMBIGUOUS"]
    pool = NodePool.model_validate(pools[0])
    if pool.backend != candidate.backend or pool.cluster_ref != cap.backend_cluster_id:
        return None, ["POOL_ROUTE_MISMATCH"]
    if pool.backend == "slurm" and not (
        pool.slurm_constraint and pool.slurm_constraint_exclusive_evidence_ref
    ):
        return None, ["POOL_SLURM_CONSTRAINT_UNVERIFIED"]
    members, reasons = [], []
    stamp = at or now()
    for ref in pool.member_capability_refs:
        item = service.store.get(conn, "capability", ref)
        if not item or item["project"] not in {project, "*"}:
            reasons.append("POOL_MEMBER_MISSING")
            continue
        member = CapabilitySnapshot.model_validate(item["body"])
        age = (stamp - member.observed_at).total_seconds()
        if any(getattr(member, k) != getattr(cap, k) for k in FINGERPRINT) or (
            member.backend != cap.backend or member.backend_cluster_id != cap.backend_cluster_id
        ):
            reasons.append("POOL_MEMBER_INCOMPATIBLE")
        elif not member.ready or age < -5 or age > member.valid_for_seconds:
            # Unready or stale members are dropped from the allowed set, never assumed.
            continue
        elif member.allocatable_count < candidate.context.resources.accelerator_count:
            continue
        else:
            members.append(member)
    if reasons:
        return None, sorted(set(reasons))
    if not members:
        return None, ["POOL_NO_VERIFIED_MEMBER"]
    target = {
        "mode": "pool",
        "pool_ref": pool.ref,
        "pool_digest": signature(pool),
        "members": [{"capability_ref": m.ref, "node_ref": m.node_ref} for m in members],
        "verification_evidence_ref": pool.verification_evidence_ref,
    }
    if pool.backend == "kubernetes":
        target["kubernetes_node_selector"] = dict(pool.kubernetes_node_selector)
    else:
        target["slurm_constraint"] = pool.slurm_constraint
        target["slurm_constraint_exclusive_evidence_ref"] = (
            pool.slurm_constraint_exclusive_evidence_ref
        )
    return target, []


def pool_availability(candidate, members, observations):
    """Any available member is enough for the native scheduler; unknown stays unknown."""
    from .scheduling import availability

    views = {m.node_ref: availability(candidate, m, observations) for m in members}
    usable = [m for m in members if views[m.node_ref]["status"] != "unavailable"]
    statuses = {views[m.node_ref]["status"] for m in usable}
    status = next((s for s in ("available", "busy", "unknown") if s in statuses), "unavailable")
    reason = {
        "available": "OBSERVED_REQUEST_HEADROOM",
        "busy": "WAIT_FOR_BACKEND_RESOURCES",
        "unknown": "NO_FRESH_INVENTORY",
        "unavailable": "OBSERVED_NODE_UNAVAILABLE",
    }[status]
    return {"status": status, "reason": reason, "members": views, "headroom": {}}, usable


def _route(candidate, cap, target):
    return target["pool_ref"] if target else cap.node_ref


def _identity(spec, candidate, cap, variant, target):
    from .policy import context_signature

    return CandidateIdentity(
        candidate.ref,
        str(candidate.backend),
        cap.backend_cluster_id,
        _route(candidate, cap, target),
        cap.resource_key or "",
        context_signature(candidate, variant),
        signature(spec.identity),
    )


def _profile_reason(p, spec, candidate, cap, variant, target, rec, at):
    from .policy import context_signature

    nodes = {m["node_ref"] for m in target["members"]} if target else {cap.node_ref}
    if p.context_signature != context_signature(candidate, variant) or (
        p.runtime_versions != cap.runtime_versions or p.environment_digest != cap.environment_digest
    ):
        return "PROFILE_RUNTIME_MISMATCH"
    h = p.hardware
    if (
        h.accelerator_vendor != cap.accelerator_vendor
        or h.accelerator_model != cap.accelerator_model
        or h.device_class != cap.device_class
        or h.memory_model != cap.memory_model
        or h.allocation_mode != cap.allocation_mode
    ):
        return "PROFILE_HARDWARE_MISMATCH"
    if (
        p.backend != candidate.backend
        or p.cluster_ref != cap.backend_cluster_id
        or p.resource_key != cap.resource_key
        or p.node_ref not in nodes
    ):
        return "PROFILE_ROUTE_MISMATCH"
    i = spec.identity
    if (
        p.precision != i.precision
        or p.batch_size != i.batch_size
        or tuple(p.input_shape) != tuple(i.input_shape)
        or p.dataset_version != i.dataset_version
    ):
        return "PROFILE_WORKLOAD_MISMATCH"
    if p.synthetic:
        return "PROFILE_SYNTHETIC"
    if not p.quality_passed:
        return "PROFILE_QUALITY_FAILED"
    age = (at - p.measured_at).total_seconds()
    if age < 0:
        return "PROFILE_FROM_FUTURE"
    if age > min(p.valid_for_seconds, rec.max_profile_age_seconds):
        return "PROFILE_STALE"
    if p.peak_memory_mib is not None and p.peak_memory_mib > spec.quality.maximum_peak_memory_mib:
        return "PROFILE_MEMORY_EXCEEDED"
    return None


def _intents(conn, project, route_key, identity, at):
    """Accepted project jobs on the route; occupancy unknown unless a plan froze it."""
    from sqlalchemy import select

    from .store import jobs

    result = []
    for job_id, state, body in conn.execute(
        select(jobs.c.id, jobs.c.state, jobs.c.body).where(jobs.c.project == project)
    ):
        if state in TERMINAL:
            continue
        plan = body.get("scheduling_plan") or {}
        cap = body.get("capability", {})
        target = plan.get("target") or {}
        key = (
            body.get("candidate", {}).get("backend"),
            cap.get("backend_cluster_id"),
            target.get("pool_ref") or cap.get("node_ref"),
        )
        if key != route_key:
            continue
        frozen = plan.get("recommendation_evidence") or {}
        result.append(
            SubmitIntent(
                identity,
                job_id,
                min(_epoch(body["created_at"]), at),
                frozen.get("selected_occupancy_seconds"),
                frozen.get("selected_admission_seconds"),
            )
        )
    return result


def _queue(service, conn, project, identity, candidate, cap):
    items = [
        x
        for x in service.store.list(conn, "route_queue_snapshot", project)
        if x["body"]["backend"] == str(candidate.backend)
        and x["body"]["cluster_ref"] == cap.backend_cluster_id
        and x["body"]["route_ref"] == identity.node_ref
        and x["body"].get("resource_key") == cap.resource_key
    ]
    latest = _latest(items)
    if latest is None:
        return None, None
    q = RouteQueueSnapshot.model_validate(latest["body"])

    def work(item):
        return WorkItem(
            item.job_id,
            item.expected_occupancy_seconds,
            started_at=item.started_at.timestamp() if item.started_at else None,
            expected_admission_seconds=item.expected_admission_seconds,
        )

    snapshot = QueueSnapshot(
        identity,
        q.ref,
        q.observed_at.timestamp(),
        q.capacity,
        complete=q.complete,
        running=tuple(work(i) for i in q.running),
        pending=tuple(work(i) for i in q.pending),
        capacity_unit=q.capacity_unit,
        observed_job_ids=tuple(q.observed_job_ids),
    )
    return snapshot, {
        "ref": q.ref,
        "digest": signature(q),
        "observed_at": q.observed_at.isoformat(),
        "complete": q.complete,
        "incomplete_reasons": list(q.incomplete_reasons),
        "source": q.source,
    }


def _transfer(location, snapshot, candidate, cap, target, at):
    """Measured link time from the data source to every allowed node (worst case)."""
    from .placement import RoleEdge
    from .placement import _transfer as link_transfer

    endpoints = {(str(e.backend), e.cluster_ref, e.node_ref): e for e in snapshot.endpoints}
    links = {(x.source_endpoint_ref, x.target_endpoint_ref): x for x in snapshot.links}
    source = next(
        (e for e in snapshot.endpoints if e.endpoint_ref == location.source_endpoint_ref), None
    )
    nodes = [m["node_ref"] for m in target["members"]] if target else [cap.node_ref]
    edge = RoleEdge(
        producer_role="data",
        consumer_role="job",
        bytes_per_transfer=location.size_bytes,
        transfer_count=1,
    )
    worst, details = 0.0, []
    for node in nodes:
        endpoint = endpoints.get((str(candidate.backend), cap.backend_cluster_id, node))

        def view(e):
            return {
                "endpoint_ref": e.endpoint_ref if e else None,
                "physical_host_ref": e.physical_host_ref if e else None,
                "backend": str(e.backend) if e else None,
                "cluster_ref": e.cluster_ref if e else None,
                "node_ref": e.node_ref if e else None,
            }

        item, *_ = link_transfer(edge, view(source), view(endpoint), links, snapshot, at)
        details.append({"node_ref": node, **item})
        if item["estimated_total_ms"] is None:
            return None, details
        worst = max(worst, item["estimated_total_ms"] / 1000)
    return worst, details


def _measured_transfer(profiles, location, candidate, cap, target, rec, at):
    """Latest fresh destination measurement per allowed node (worst member for pools).

    The latest observation of a node reflects its observed cache state (cold or
    hot); a hot cache is never assumed without a verified read.
    """
    from .network_profile import summary

    nodes = [m["node_ref"] for m in target["members"]] if target else [cap.node_ref]
    detail = {"source": "measured_network", "per_node": [], "reason": None}
    if location.content_digest is None:
        return None, {**detail, "reason": "DATA_DIGEST_UNKNOWN"}
    worst = None
    for node in nodes:
        mine = sorted(
            (
                p
                for p in profiles
                if p.workload_ref == location.workload_ref
                and p.dataset_version == location.dataset_version
                and p.source_endpoint_ref == location.source_endpoint_ref
                and str(p.backend) == str(candidate.backend)
                and p.cluster_ref == cap.backend_cluster_id
                and p.node_ref == node
            ),
            key=lambda p: (p.observed_at, p.ref),
            reverse=True,
        )
        if not mine:
            return None, {**detail, "reason": "NETWORK_PROFILE_MISSING", "node_ref": node}
        p = mine[0]
        age = (at - p.observed_at).total_seconds()
        reason = (
            "NETWORK_DATASET_MISMATCH"
            if p.content_digest != location.content_digest or p.size_bytes != location.size_bytes
            else "NETWORK_PROFILE_FROM_FUTURE"
            if age < -5
            else "NETWORK_PROFILE_STALE"
            if age > min(p.valid_for_seconds, rec.max_profile_age_seconds)
            else None
        )
        if reason:
            return None, {**detail, "reason": reason, "node_ref": node, "profile_ref": p.ref}
        stats = summary(p)
        detail["per_node"].append(
            {
                "node_ref": node,
                "ref": p.ref,
                "digest": signature(p),
                "cache_state": p.cache_state,
                "observed_at": p.observed_at.isoformat(),
                "age_seconds": age,
                **stats,
            }
        )
        if worst is None or stats["median_seconds"] > worst["median_seconds"]:
            worst = {**stats, "cache_state": p.cache_state}
    return worst["median_seconds"], {**detail, **worst}


PREPARATION = ("runtime_startup", "model_preparation")
OTHER = ("communication", "finalization", "unattributed_in_container")


def breakdown(prediction, profile_ref, staging):
    """Separate cost components and what is (not) known about their spread.

    Spread is observed and descriptive (sample stddev or min/max); no component
    is calibrated, and a single sample or a point queue estimate is listed as
    missing spread rather than reported as zero uncertainty.
    """
    phases = dict(prediction.phase_seconds)
    missing = []
    if prediction.predicted_jct_seconds is None:
        return None
    if prediction.queue_wait_seconds is not None and prediction.queue_snapshot_ref:
        missing.append("queue_wait_spread")
    if prediction.admission_seconds is not None:
        missing.append("admission_spread")
    if profile_ref["repeats"] < 2 or prediction.timing_uncertainty_seconds is None:
        missing.append("profile_spread")
    if staging["source"] == "measured_network" and staging["samples"] < 2:
        missing.append("staging_spread")
    return {
        "queue_wait_seconds": prediction.queue_wait_seconds,
        "admission_seconds": prediction.admission_seconds,
        "preparation_seconds": sum(phases.get(k, 0.0) for k in PREPARATION),
        "staging": {**staging, "seconds": phases.get("data_staging")},
        "compute_seconds": phases.get("compute"),
        "other_seconds": sum(phases.get(k, 0.0) for k in OTHER),
        "phase_seconds": phases,
        "critical_path_seconds": prediction.critical_path_seconds,
        "combination": "sequential sum; each overlap group adds only its longest phase",
        "overlap_groups": profile_ref.get("overlap_groups", []),
        "uncertainty": {
            "calibrated": False,
            "kind": "observed descriptive spread; not a confidence interval",
            "profile_repeats": profile_ref["repeats"],
            "profile_jct_stddev_seconds": prediction.timing_uncertainty_seconds,
            "staging_range_seconds": [staging["min_seconds"], staging["max_seconds"]]
            if staging.get("samples", 0) >= 2
            else None,
            "missing": missing,
        },
    }


def rank(service, conn, project, spec, rec, eligible, *, at=None):
    """Select among hard-filtered candidates; returns (index | None, record, evidence)."""
    from .placement import TopologySnapshot, snapshot_view
    from .service import required

    at = at or now()
    stamp = at.timestamp()
    identities, profiles, queues, intents, notes = [], [], [], [], {}
    profile_refs, queue_refs, transfers, transfer_details = {}, {}, {}, {}
    location, location_reason = data_location(service, conn, project, spec)
    topology = None
    measured = rec.version == MEASURED_VERSION
    network_profiles, network_refs = [], {}
    if measured and rec.policy == "profile_queue_network":
        from .network_profile import NetworkProfile

        network_profiles = [
            NetworkProfile.model_validate(x["body"])
            for x in service.store.list(conn, "network_profile", project)
        ]
    elif rec.policy == "profile_queue_network":
        topology = TopologySnapshot.model_validate(
            required(service.store, conn, "topology_snapshot", rec.topology_snapshot_ref, project)
        )
    all_profiles = [
        JctProfile.model_validate(x["body"])
        for x in service.store.list(conn, "jct_profile", project)
        if x["body"]["workload_signature"] == signature(spec.identity)
    ]
    for item in eligible:
        candidate, cap, variant, target = (
            item["candidate"],
            item["cap"],
            item["variant"],
            item["target"],
        )
        identity = _identity(spec, candidate, cap, variant, target)
        identities.append(identity)
        mine = sorted(
            (p for p in all_profiles if p.candidate_ref == candidate.ref),
            key=lambda p: (p.measured_at, p.ref),
            reverse=True,
        )
        checked = [
            (p, _profile_reason(p, spec, candidate, cap, variant, target, rec, at)) for p in mine
        ]
        valid = [p for p, reason in checked if reason is None]
        if not valid:
            notes[candidate.ref] = checked[0][1] if checked else "PROFILE_MISSING"
            continue
        p = valid[0]
        phases = dict(p.phases)
        if p.staging_boundary == "not_required":
            phases["data_staging"] = 0.0
        profile_refs[candidate.ref] = {
            "ref": p.ref,
            "digest": signature(p),
            "measured_at": p.measured_at.isoformat(),
            "repeats": p.repeats,
            "node_ref": p.node_ref,
            "staging_boundary": p.staging_boundary,
            "overlap_groups": [list(g) for g in p.overlap_groups],
        }
        profiles.append(
            MeasuredProfile(
                identity=identity,
                evidence_ref=p.ref,
                measured_at=p.measured_at.timestamp(),
                compute_seconds=None,
                preparation_seconds=None,
                admission_seconds=p.admission_seconds,
                release_seconds=None,
                v2_service_seconds=None,
                qualified=True,
                quality_passed=p.quality_passed,
                synthetic=p.synthetic,
                readmission_seconds=p.readmission_seconds,
                timing_uncertainty_seconds=p.jct_stddev_seconds,
                timing_notes=(f"repeats={p.repeats}", "descriptive stddev, not calibrated"),
                phases=tuple(phases.items()),
                overlap_groups=tuple(tuple(g) for g in p.overlap_groups),
            )
        )
        if rec.policy in {"profile_queue", "profile_queue_network"}:
            snapshot, view = _queue(service, conn, project, identity, candidate, cap)
            if snapshot is not None:
                queues.append(snapshot)
                queue_refs[candidate.ref] = view
            route_key = (str(candidate.backend), cap.backend_cluster_id, identity.node_ref)
            intents.extend(_intents(conn, project, route_key, identity, stamp))
        if rec.policy == "profile_queue_network":
            if p.staging_boundary == "not_required":
                transfers[candidate.ref] = 0.0
                transfer_details[candidate.ref] = "DATA_NOT_REQUIRED"
            elif location is None:
                transfers[candidate.ref] = None
                transfer_details[candidate.ref] = location_reason or "DATA_LOCATION_MISSING"
            elif measured:
                # Staging inside compute abstains in select_candidate (never added).
                transfers[candidate.ref], transfer_details[candidate.ref] = _measured_transfer(
                    network_profiles, location, candidate, cap, target, rec, at
                )
                network_refs[candidate.ref] = transfer_details[candidate.ref]["per_node"]
            else:
                transfers[candidate.ref], transfer_details[candidate.ref] = _transfer(
                    location, topology, candidate, cap, target, at
                )
    ranked = [i for i in identities if i.candidate_ref not in notes]
    decision = select_candidate(
        rec.policy,
        ranked,
        profiles,
        now_seconds=stamp,
        queues=queues,
        max_queue_age_seconds=rec.max_queue_age_seconds,
        max_profile_age_seconds=rec.max_profile_age_seconds,
        intents=intents,
        transfer_seconds=transfers,
    )
    views = []
    for prediction in decision.predictions:
        view = asdict(prediction)
        detail = transfer_details.get(prediction.candidate_ref)
        if isinstance(detail, dict) and detail.get("reason"):
            view["reasons"] = list(view["reasons"]) + [detail["reason"]]
        staging = (
            detail
            if isinstance(detail, dict) and detail.get("source") == "measured_network"
            else {
                "source": "not_required"
                if profile_refs.get(prediction.candidate_ref, {}).get("staging_boundary")
                == "not_required"
                else "topology_link"
                if rec.policy == "profile_queue_network"
                else "profile_phase",
                "samples": 0,
            }
        )
        if staging.get("source") == "measured_network":
            staging = {
                **{k: v for k, v in staging.items() if k not in {"per_node", "reason"}},
                "profile_refs": [n["ref"] for n in staging["per_node"]],
                "per_node": staging["per_node"],
            }
        view["breakdown"] = (
            breakdown(prediction, profile_refs[prediction.candidate_ref], staging)
            if prediction.candidate_ref in profile_refs
            else None
        )
        views.append(view)
    predictions = views + [
        {"candidate_ref": ref, "predicted_jct_seconds": None, "reasons": [reason]}
        for ref, reason in sorted(notes.items())
    ]
    selected = decision.candidate_ref
    chosen = next((p for p in decision.predictions if p.candidate_ref == selected), None)
    reasons = {r for p in predictions for r in p["reasons"]} if selected is None else set()
    status = (
        "SELECTED"
        if selected
        else "NEEDS_PROFILE"
        if reasons and all(r.startswith("PROFILE_") for r in reasons)
        else "ESTIMATE_UNAVAILABLE"
    )
    record = {
        "version": rec.version,
        "policy": rec.policy,
        "status": status,
        "selected_candidate_ref": selected,
        "fallback": None,
        "profiles": profile_refs,
        "data_location": {"ref": location.ref, "digest": signature(location)} if location else None,
        "topology_snapshot": {"ref": topology.ref, "digest": signature(topology)}
        if topology
        else None,
    }
    if measured:
        record["network_profiles"] = {
            ref: [{"ref": n["ref"], "digest": n["digest"]} for n in nodes]
            for ref, nodes in sorted(network_refs.items())
        }
    evidence = {
        "evaluated_at": at.isoformat(),
        "predictions": predictions,
        "queue_snapshots": queue_refs,
        "transfers": transfer_details,
        "topology": snapshot_view(topology, at) if topology else None,
        "selected_occupancy_seconds": chosen.critical_path_seconds if chosen else None,
        "selected_admission_seconds": chosen.admission_seconds if chosen else None,
        "uncertainty": "point estimate with descriptive profile stddev; no calibrated confidence",
    }
    index = next((n for n, item in enumerate(eligible) if item["candidate"].ref == selected), None)
    return index, record, evidence


def import_evidence(service, project, kind, model):
    """Immutable per ref; observations from the future are rejected, never clamped."""
    from .service import Rejected

    stamp = getattr(model, "measured_at", None) or getattr(model, "observed_at", None)
    if stamp is not None and (stamp - now()).total_seconds() > 5:
        raise Rejected(f"{kind} timestamp is in the future")
    return service.register(kind, model, project)


def decision_linkage(service, project, job_id):
    """Saved decision → accepted plan → native Job identity → actual placement."""
    from .service import NotFound

    with service.store.transaction() as conn:
        row = service.store.job(conn, job_id)
        if not row or row["project"] != project:
            raise NotFound("job not found")
        body = row["body"]
        plan = body.get("scheduling_plan") or {}
        return {
            "job_id": row["id"],
            "state": row["state"],
            "native_job_id": body.get("external_id"),
            "backend": body["candidate"]["backend"],
            "cluster_ref": body.get("backend_cluster_id"),
            "candidate_ref": body["candidate"]["ref"],
            "scheduling_plan_digest": body.get("scheduling_plan_digest"),
            "profile_ref": plan.get("profile_ref"),
            "profile_version": plan.get("profile_version"),
            "recommendation": plan.get("recommendation"),
            "recommendation_evidence": plan.get("recommendation_evidence"),
            "target": plan.get("target") or {"mode": "node", "node_ref": plan.get("node_ref")},
            "excluded": [e for e in plan.get("evaluation", []) if not e.get("eligible")],
            "placement": body.get("placement"),
            "allocation": body.get("allocation"),
            "native_timestamps": {
                k: body.get(k)
                for k in (
                    "created_at",
                    "scheduler_submitted_at",
                    "started_at",
                    "execution_started_at",
                    "backend_finished_at",
                )
            },
            "error": body.get("error"),
            "quality_passed": body.get("quality_passed"),
            "actual_capability_ref": body.get("actual_capability_ref"),
            "profile_excluded": body.get("profile_excluded"),
            "outcome": (service.store.get(conn, "jct_outcome", body["attempt_id"]) or {}).get(
                "body"
            ),
            "comparison": _comparison_arm(row["idempotency_key"]),
        }


def _comparison_arm(key):
    if not (key or "").startswith("cmp:"):
        return None
    ref, _, arm = key[4:].rpartition(":")
    return {"ref": ref, "arm": arm} if ref and arm in COMPARISON_ARMS else None


def record_outcome(store, conn, row, state, body):
    """Immutable frozen-prediction vs native-JCT record for a scheduled terminal attempt.

    The prediction is the one frozen in the accepted plan before submission. Actual
    JCT is native submit → native finish, the span the estimate covers (queue wait,
    admission and allocation phases). Non-success or missing timestamps keep a
    status and reason; no JCT or error is fabricated.
    """
    from .contracts import State

    plan = body.get("scheduling_plan")
    if not plan:
        return
    rec = plan.get("recommendation") or {}
    evidence = plan.get("recommendation_evidence") or {}
    selected = plan["candidate_ref"]
    chosen = next(
        (p for p in evidence.get("predictions", []) if p["candidate_ref"] == selected), None
    )
    predicted = (chosen or {}).get("predicted_jct_seconds")
    submitted, finished = body.get("scheduler_submitted_at"), body.get("backend_finished_at")
    jct, reason = None, None
    if state != State.SUCCEEDED:
        status, reason = "NOT_COMPLETED", f"{state}:{body.get('error') or 'NO_ERROR_RECORDED'}"
    elif not submitted or not finished:
        status, reason = "TIMESTAMPS_MISSING", "NATIVE_SUBMIT_OR_FINISH_UNOBSERVED"
    else:
        jct = _epoch(finished) - _epoch(submitted)
        status = "OBSERVED" if jct >= 0 else "TIMESTAMPS_INVALID"
        if jct < 0:
            jct, reason = None, "NATIVE_FINISH_BEFORE_SUBMIT"
    error = None
    if jct is not None and predicted is not None:
        signed = jct - predicted
        error = {
            "signed_seconds": signed,
            "absolute_seconds": abs(signed),
            "relative": signed / jct if jct > 0 else None,
            "convention": "actual - predicted; positive means slower than predicted",
        }
    outcome = {
        "job_id": row["id"],
        "attempt_id": body["attempt_id"],
        "state": str(state),
        "policy": rec.get("policy", "legacy"),
        "version": rec.get("version"),
        "recommendation_status": rec.get("status"),
        "candidate_ref": selected,
        "scheduling_plan_digest": body.get("scheduling_plan_digest"),
        "plan_reasons": plan.get("reason", []),
        "prediction": None
        if predicted is None
        else {
            "predicted_jct_seconds": predicted,
            "evaluated_at": evidence.get("evaluated_at"),
            "frozen": "in the accepted plan before native submission",
            "reasons": chosen.get("reasons", []),
            "breakdown": chosen.get("breakdown"),
        },
        "prediction_reason": None if predicted is not None else "PREDICTION_UNAVAILABLE",
        "actual": {
            "status": status,
            "reason": reason,
            "submitted_at": submitted,
            "finished_at": finished,
            "jct_seconds": jct,
            "boundary": "native scheduler_submitted_at → backend_finished_at",
        },
        "error": error,
    }
    store.put(conn, "jct_outcome", body["attempt_id"], row["project"], outcome)


# --- Four-policy comparison: frozen shadow estimates, then independent executions. ---

COMPARISON_ARMS = ("static", "profile_only", "profile_queue", "profile_queue_network")


class RecommendationComparisonRequest(Contract):
    ref: Ref
    workload_ref: Ref
    static_profile_ref: Ref
    static_candidate_ref: Ref
    profile_refs: dict[Literal["profile_only", "profile_queue", "profile_queue_network"], Ref]

    @model_validator(mode="after")
    def every_policy_arm(self):
        if set(self.profile_refs) != set(COMPARISON_ARMS[1:]):
            raise ValueError("profile_only, profile_queue and profile_queue_network are required")
        return self


def comparison_key(ref, arm):
    """Idempotency key that links an executed job to its comparison arm."""
    return f"cmp:{ref}:{arm}"


def freeze_comparison(service, project, request):
    """Evaluate all four arms in one transaction and store them before any submission."""
    from .scheduling import SchedulingPlanRequest, SchedulingProfile, compile_plan
    from .service import Rejected, required
    from .store import Conflict

    with service.store.transaction() as conn:
        existing = service.store.get(conn, "recommendation_comparison", request.ref)
        if existing and existing["project"] == project:
            if existing["body"]["request"] != request.model_dump(mode="json"):
                raise Conflict("comparison reference reused with a different request")
            return _comparison_view(service, conn, project, existing)
        arms = {}
        for arm in COMPARISON_ARMS:
            static = arm == "static"
            profile_ref = request.static_profile_ref if static else request.profile_refs[arm]
            profile = SchedulingProfile.model_validate(
                required(service.store, conn, "scheduling_profile", profile_ref, project)
            )
            rec = profile.policy.recommendation
            if static != (rec is None) or (rec is not None and rec.policy != arm):
                raise Rejected(f"{arm} arm requires a matching {arm} scheduling profile")
            try:
                result = compile_plan(
                    service,
                    conn,
                    project,
                    SchedulingPlanRequest(
                        profile_ref=profile_ref,
                        workload_ref=request.workload_ref,
                        candidate_ref=request.static_candidate_ref if static else None,
                    ),
                )
            except ValueError as exc:
                raise Rejected(str(exc)) from exc
            if static and not result["accepted"]:
                raise Rejected("STATIC_CANDIDATE_NOT_ELIGIBLE: " + str(result["excluded"]))
            plan = result["plan"] or {}
            evidence = plan.get("recommendation_evidence") or result.get("recommendation_evidence")
            arms[arm] = {
                "profile_ref": profile_ref,
                "policy": rec.policy if rec else "static",
                "version": rec.version if rec else None,
                "accepted": result["accepted"],
                "status": (plan.get("recommendation") or result.get("recommendation") or {}).get(
                    "status", "STATIC" if static else None
                ),
                "selected_candidate_ref": plan.get("candidate_ref"),
                "digest": result["digest"],
                "reasons": result["reasons"],
                "excluded": result["excluded"],
                "candidate_pool": [
                    e["candidate_ref"] for e in plan.get("evaluation", result.get("evaluation", []))
                ],
                "evaluated_at": (evidence or {}).get("evaluated_at"),
                "predictions": (evidence or {}).get("predictions", []),
            }
        body = {
            "ref": request.ref,
            "project_ref": project,
            "request": request.model_dump(mode="json"),
            "kind": "shadow_estimates_frozen_before_submission",
            "frozen_at": now().isoformat(),
            "workload_ref": request.workload_ref,
            "arm_order": list(COMPARISON_ARMS),
            "arms": arms,
            "claims": (
                "Shadow estimates cover every candidate under each policy at one instant. "
                "Executed outcomes are separate jobs run independently per arm; no "
                "counterfactual measured performance is inferred from shadow estimates."
            ),
        }
        saved = service.store.put(conn, "recommendation_comparison", request.ref, project, body)
        return _comparison_view(service, conn, project, saved)


def comparison(service, project, ref):
    from .service import NotFound

    with service.store.transaction() as conn:
        item = service.store.get(conn, "recommendation_comparison", ref)
        if not item or item["project"] != project:
            raise NotFound("comparison not found")
        return _comparison_view(service, conn, project, item)


def _comparison_view(service, conn, project, item):
    from sqlalchemy import select

    from .store import jobs

    body = item["body"]
    executed = {}
    for arm in body["arm_order"]:
        rows = conn.execute(
            select(jobs).where(
                jobs.c.project == project,
                jobs.c.idempotency_key == comparison_key(body["ref"], arm),
            )
        ).mappings()
        frozen = body["arms"][arm]["selected_candidate_ref"]
        executed[arm] = [
            {
                "kind": "independently_executed",
                "job_id": row["id"],
                "state": row["state"],
                "candidate_ref": row["body"]["candidate"]["ref"],
                "matches_frozen_selection": row["body"]["candidate"]["ref"] == frozen,
                "scheduling_plan_digest": row["body"].get("scheduling_plan_digest"),
                "outcome": (
                    service.store.get(conn, "jct_outcome", row["body"]["attempt_id"]) or {}
                ).get("body"),
            }
            for row in rows
        ]
    return {
        **body,
        "arms": {arm: body["arms"][arm] for arm in body["arm_order"]},
        "frozen_digest": item["digest"],
        "executed": executed,
    }


# --- Collectors: pure transforms over read-only native observations. ---------------


def jct_profile_from_jobs(rows, *, ref, valid_for_seconds, hardware, load=None):
    """Aggregate COMPLETED, quality-passed RA jobs with native timestamps on one node.

    Each row is a job record plus ``result``: its stored ExecutionResult body, and
    optionally ``staging``: its stored StagingReceipt body.

    Phases come only from native boundaries: admission = submit→allocation start,
    runtime_startup = allocation start→container start, compute = workload-reported
    elapsed, unattributed_in_container = remaining container time. Without staging
    receipts staging is inside compute (no separate boundary), so network
    substitution abstains. When every job staged its dataset before compute and
    reported a verified receipt measured on its allocated node, ``data_staging`` is
    that separate span and is subtracted from the residual (never counted twice).
    """
    import statistics

    from .network_profile import StagingReceipt

    rows = list(rows)
    if not rows:
        raise ValueError("no completed jobs")
    first = rows[0]["body"]
    keys = ("workload_signature", "context_signature")
    samples = []
    staged = {row.get("staging") is not None for row in rows}
    if len(staged) > 1:
        raise ValueError("mixed staged and unstaged jobs: staging boundaries differ")
    staged = staged.pop()
    for row in rows:
        b = row["body"]
        if row["state"] != "SUCCEEDED" or not b.get("quality_passed"):
            raise ValueError(f"{row['id']}: only quality-passed SUCCEEDED jobs are profiles")
        if (
            any(b[k] != first[k] for k in keys)
            or b["candidate"]["ref"] != first["candidate"]["ref"]
        ):
            raise ValueError(f"{row['id']}: mixed workload/runtime identity")
        node = (b.get("placement") or {}).get("node_name")
        if node is None or node != first.get("placement", {}).get("node_name"):
            raise ValueError(f"{row['id']}: actual allocated node unknown or mixed")
        result = row["result"]
        if (
            result["evidence_kind"] != "hardware"
            or result["outcome"] != "COMPLETED"
            or result["attempt_id"] != b["attempt_id"]
            or signature(result) != b.get("result_digest")
        ):
            raise ValueError(f"{row['id']}: result is synthetic, incomplete or unrelated")
        stamps = [
            b.get(k)
            for k in (
                "scheduler_submitted_at",
                "started_at",
                "execution_started_at",
                "backend_finished_at",
            )
        ]
        if any(s is None for s in stamps):
            raise ValueError(f"{row['id']}: native timestamps incomplete")
        submitted, started, executing, finished = (_epoch(s) for s in stamps)
        compute = result["measurements"]["elapsed_seconds"]
        staging = None
        if staged:
            receipt = StagingReceipt.model_validate(row["staging"])
            first_receipt = StagingReceipt.model_validate(rows[0]["staging"])
            if (
                receipt.attempt_id != b["attempt_id"]
                or receipt.observer_host != node
                or (receipt.content_digest, receipt.size_bytes)
                != (first_receipt.content_digest, first_receipt.size_bytes)
            ):
                raise ValueError(f"{row['id']}: staging receipt unrelated, remote or mixed dataset")
            staging = receipt.sample.elapsed_seconds
        residual = finished - executing - compute - (staging or 0.0)
        if not submitted <= started <= executing <= finished or residual < 0:
            raise ValueError(f"{row['id']}: native boundaries overlap the measured compute span")
        samples.append(
            {
                "admission": started - submitted,
                "runtime_startup": executing - started,
                "compute": compute,
                "unattributed_in_container": residual,
                "data_staging": staging,
                "total": finished - submitted,
                "peak": result["measurements"]["peak_memory_mib"],
                "finished": finished,
            }
        )

    def mean(key):
        return statistics.fmean(s[key] for s in samples)

    spec, cap = first["spec"], first["capability"]
    identity = spec["identity"]
    return JctProfile(
        ref=ref,
        project_ref=spec["project_ref"],
        workload_ref=spec["ref"],
        candidate_ref=first["candidate"]["ref"],
        backend=first["candidate"]["backend"],
        cluster_ref=cap["backend_cluster_id"],
        node_ref=first["placement"]["node_name"],
        resource_key=cap["resource_key"],
        workload_signature=first["workload_signature"],
        context_signature=first["context_signature"],
        environment_digest=cap["environment_digest"],
        runtime_versions=cap["runtime_versions"],
        hardware=hardware,
        precision=identity["precision"],
        batch_size=identity["batch_size"],
        input_shape=tuple(identity["input_shape"]),
        dataset_version=identity["dataset_version"],
        measured_at=datetime.fromtimestamp(max(s["finished"] for s in samples), tz=now().tzinfo),
        valid_for_seconds=valid_for_seconds,
        repeats=len(samples),
        admission_seconds=mean("admission"),
        phases={
            k: mean(k)
            for k in ("runtime_startup", "compute", "unattributed_in_container")
            + (("data_staging",) if staged else ())
        },
        staging_boundary="separate" if staged else "inside_compute",
        jct_stddev_seconds=statistics.stdev(s["total"] for s in samples)
        if len(samples) > 1
        else None,
        peak_memory_mib=max(s["peak"] for s in samples),
        load=dict(load or {}),
        quality_passed=True,
        synthetic=False,
        job_ids=tuple(row["id"] for row in rows),
        source="ra_jobs",
    )


def queue_snapshot_from_pods(
    pods,
    *,
    ref,
    project_ref,
    cluster_ref,
    route_ref,
    nodes,
    resource_key,
    capacity,
    capacity_unit,
    observed_at,
    evidence_ref,
    expected_occupancy=None,
):
    """Kubernetes Pods consuming ``resource_key`` on the route's nodes.

    A Pod's expected occupancy is known only through ``expected_occupancy`` (job id
    → seconds, e.g. a frozen RA plan). External long-running occupants stay unknown,
    which marks the snapshot incomplete instead of pretending the device is free.
    """
    from .inventory import pod_request

    expected_occupancy = expected_occupancy or {}
    running, pending, reasons = [], [], []
    for pod in pods:
        spec, status = pod.get("spec", {}), pod.get("status", {})
        if status.get("phase") in {"Succeeded", "Failed"}:
            continue
        if not resource_key or not pod_request(pod, resource_key):
            continue
        node = spec.get("nodeName")
        if node is not None and node not in nodes:
            continue
        job_id = pod["metadata"].get("annotations", {}).get("resource-advisor/job-id")
        name = (
            job_id
            or "external:" + pod["metadata"].get("namespace", "") + "/" + pod["metadata"]["name"]
        )
        occupancy = expected_occupancy.get(job_id) if job_id else None
        if occupancy is None:
            reasons.append("OCCUPANCY_UNKNOWN:" + name)
        started = status.get("startTime")
        item = QueueItem(
            job_id=name[:128], expected_occupancy_seconds=occupancy, started_at=started
        )
        (running if node is not None else pending).append(item)
    return RouteQueueSnapshot(
        ref=ref,
        project_ref=project_ref,
        backend="kubernetes",
        cluster_ref=cluster_ref,
        route_ref=route_ref,
        resource_key=resource_key,
        observed_at=observed_at,
        complete=not reasons,
        incomplete_reasons=tuple(reasons),
        capacity=capacity,
        capacity_unit=capacity_unit,
        running=tuple(running),
        pending=tuple(pending),
        source="kubernetes:pods",
        evidence_ref=evidence_ref,
    )
