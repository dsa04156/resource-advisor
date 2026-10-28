"""Bounded role-graph placement over registered candidates and measured links.

Planning never reserves, submits or authorizes a runtime. A topology snapshot is
operator-imported evidence: only directed links that were measured are used, and
missing links stay unknown instead of becoming zero-cost or "fast". Transfer
costs are labelled estimates, not training time or proof of optimal hardware.
"""

import itertools
import math
from collections import Counter
from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .contracts import Backend, Contract, Ref, Resources, WorkloadSpec, now, signature

MAX_ROLES = 4
MAX_CANDIDATES_PER_ROLE = 8
MAX_COMBINATIONS = 4096
MAX_SNAPSHOT_TTL = timedelta(days=7)
FUTURE_SKEW = timedelta(seconds=5)
LISTED = 64

SEMANTICS = (
    "Accepted means a feasible preview, not an atomic group reservation or a submitted job.",
    "No group/multi-stage executor is provided; each role is submitted and rechecked by its own runtime.",
    "Communication cost is an ESTIMATE: sum(count * (request_overhead_ms + bytes / bandwidth * 1000)) over measured directed links.",
    "Estimates are not training time and do not claim optimal hardware performance.",
    "Missing, reverse-only or expired links are unknown, never zero cost. Same host is a label, not a free link.",
    "Topology is planning evidence only and does not authorize a runtime.",
)


class TopologyDevice(Contract):
    device_ref: Ref
    resource_key: str = Field(min_length=1, max_length=128)


class TopologyEndpoint(Contract):
    endpoint_ref: Ref
    backend: Backend
    cluster_ref: Ref
    node_ref: Ref
    physical_host_ref: Ref | None = None
    devices: tuple[TopologyDevice, ...] = Field(default=(), max_length=64)


class TopologyLink(Contract):
    source_endpoint_ref: Ref
    target_endpoint_ref: Ref
    bandwidth_bytes_per_second: float = Field(gt=0, le=1e13)
    request_overhead_ms: float = Field(ge=0, le=3_600_000)
    measured_at: AwareDatetime
    evidence_ref: Ref


class TopologySnapshot(Contract):
    ref: Ref
    project_ref: Ref
    source: Literal["operator_import"] = "operator_import"
    evidence_ref: Ref
    measurement_method: str = Field(min_length=1, max_length=256)
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    link_max_age_seconds: int = Field(default=3600, ge=1, le=86400)
    endpoints: tuple[TopologyEndpoint, ...] = Field(min_length=1, max_length=64)
    links: tuple[TopologyLink, ...] = Field(default=(), max_length=1024)

    @model_validator(mode="after")
    def unambiguous(self):
        if not self.observed_at < self.expires_at <= self.observed_at + MAX_SNAPSHOT_TTL:
            raise ValueError("expires_at must follow observed_at within seven days")
        refs = [e.endpoint_ref for e in self.endpoints]
        identities = [(e.backend, e.cluster_ref, e.node_ref) for e in self.endpoints]
        if len(set(refs)) != len(refs) or len(set(identities)) != len(identities):
            raise ValueError("duplicate topology endpoint")
        owners = {}
        for item in self.endpoints:
            keys = [d.device_ref for d in item.devices]
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate device on endpoint")
            for device in item.devices:
                owners.setdefault(device.device_ref, []).append(item)
        for device, items in owners.items():
            hosts = {e.physical_host_ref for e in items}
            if len(items) > 1 and (None in hosts or len(hosts) != 1):
                raise ValueError(f"device {device} aliased without one shared physical host")
            if (
                len({d.resource_key for e in items for d in e.devices if d.device_ref == device})
                > 1
            ):
                raise ValueError(f"device {device} has conflicting resource keys")
        pairs = [(x.source_endpoint_ref, x.target_endpoint_ref) for x in self.links]
        if len(set(pairs)) != len(pairs):
            raise ValueError("duplicate ambiguous directed link")
        if any(ref not in refs for pair in pairs for ref in pair):
            raise ValueError("link references an unknown endpoint")
        age = timedelta(seconds=self.link_max_age_seconds)
        if any(not self.observed_at - age <= x.measured_at <= self.observed_at for x in self.links):
            raise ValueError("link measurement must lie within link_max_age before observed_at")
        return self


class PlacementRole(Contract):
    role_ref: Ref
    workload_ref: Ref
    candidate_refs: tuple[Ref, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_CANDIDATES_PER_ROLE
    )


class RoleEdge(Contract):
    producer_role: Ref
    consumer_role: Ref
    bytes_per_transfer: int = Field(ge=0, le=10**15)
    transfer_count: int = Field(ge=1, le=10**9)
    requirement: Literal["soft", "hard"] = "soft"
    same_node: bool = False
    min_bandwidth_bytes_per_second: float | None = Field(default=None, gt=0, le=1e13)
    max_request_overhead_ms: float | None = Field(default=None, ge=0, le=3_600_000)


class PlacementRequest(Contract):
    task_ref: Ref
    scheduling_profile_ref: Ref
    topology_snapshot_ref: Ref
    roles: tuple[PlacementRole, ...] = Field(min_length=1, max_length=MAX_ROLES)
    edges: tuple[RoleEdge, ...] = Field(default=(), max_length=MAX_ROLES * (MAX_ROLES - 1) // 2)
    require_distinct_physical_devices: bool = True

    @model_validator(mode="after")
    def bounded_dag(self):
        roles = [r.role_ref for r in self.roles]
        if len(set(roles)) != len(roles):
            raise ValueError("duplicate role")
        for r in self.roles:
            if r.candidate_refs and len(set(r.candidate_refs)) != len(r.candidate_refs):
                raise ValueError("duplicate candidate restriction")
        pairs = [(e.producer_role, e.consumer_role) for e in self.edges]
        if len(set(pairs)) != len(pairs):
            raise ValueError("duplicate role edge")
        for a, b in pairs:
            if a not in roles or b not in roles:
                raise ValueError("edge references an unknown role")
            if a == b:
                raise ValueError("role graph contains a cycle")
        # Kahn order over at most four roles.
        incoming = {r: {a for a, b in pairs if b == r} for r in roles}
        done = set()
        while len(done) < len(roles):
            ready = [r for r in roles if r not in done and incoming[r] <= done]
            if not ready:
                raise ValueError("role graph contains a cycle")
            done.update(ready)
        return self


class ResolvedCandidate(Contract):
    role_ref: Ref
    workload_ref: Ref
    candidate_ref: Ref
    backend: Backend
    cluster_ref: Ref | None = None
    node_ref: Ref | None = None
    device_class: Literal["gpu", "npu", "cpu"] | None = None
    accelerator_model: str | None = Field(default=None, max_length=256)
    resource_key: str | None = Field(default=None, max_length=128)
    allocation_mode: Literal["physical_device", "virtual_slot", "cpu_only"]
    resources: Resources
    availability: Literal["available", "busy", "unknown", "unavailable"]
    headroom: dict[str, float | None] = Field(default_factory=dict)
    reasons: tuple[str, ...] = ()


def snapshot_view(snapshot, at):
    if snapshot is None:
        return {"status": "missing", "ref": None, "digest": None}
    return {
        "status": "fresh" if snapshot.observed_at <= at < snapshot.expires_at else "expired",
        "ref": snapshot.ref,
        "digest": signature(snapshot),
        "source": snapshot.source,
        "evidence_ref": snapshot.evidence_ref,
        "measurement_method": snapshot.measurement_method,
        "observed_at": snapshot.observed_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
        "age_seconds": round((at - snapshot.observed_at).total_seconds(), 3),
    }


def _ms(value):
    return round(value, 6)


def _candidate_view(c, endpoint, snapshot_ok):
    devices = (
        sorted(d.device_ref for d in endpoint.devices if d.resource_key == c.resource_key)
        if endpoint
        else []
    )
    eligible = not c.reasons and c.availability != "unavailable"
    reasons = list(c.reasons)
    if c.availability == "unavailable" and not reasons:
        reasons.append("CANDIDATE_UNAVAILABLE")
    return {
        "role_ref": c.role_ref,
        "workload_ref": c.workload_ref,
        "candidate_ref": c.candidate_ref,
        "backend": str(c.backend),
        "cluster_ref": c.cluster_ref,
        "node_ref": c.node_ref,
        "device_class": c.device_class,
        "accelerator_model": c.accelerator_model,
        "resource_key": c.resource_key,
        "allocation_mode": c.allocation_mode,
        "resources": c.resources.model_dump(mode="json"),
        "availability": c.availability,
        "headroom": dict(sorted(c.headroom.items())),
        "endpoint_ref": endpoint.endpoint_ref if endpoint else None,
        "physical_host_ref": endpoint.physical_host_ref if endpoint else None,
        "device_refs": devices if devices else None,
        "topology": "matched" if endpoint else ("not_in_snapshot" if snapshot_ok else "missing"),
        "eligible": eligible,
        "reasons": reasons,
    }


def _transfer(edge, producer, consumer, links, snapshot, at):
    src, dst = producer["endpoint_ref"], consumer["endpoint_ref"]
    if src is None or dst is None:
        status = "endpoint_unknown" if snapshot is not None else "missing"
        found = None
    else:
        found = links.get((src, dst))
        status = "missing" if found is None else "measured"
        # Link age is bounded on its own; a fresh snapshot cannot launder old probes.
        if found is not None and (
            not snapshot.observed_at <= at < snapshot.expires_at
            or (at - found.measured_at).total_seconds() > snapshot.link_max_age_seconds
        ):
            status = "expired"
    same_host = None
    if producer["physical_host_ref"] and consumer["physical_host_ref"]:
        same_host = producer["physical_host_ref"] == consumer["physical_host_ref"]
    same_node = (
        producer["backend"] == consumer["backend"]
        and producer["cluster_ref"] == consumer["cluster_ref"]
        and producer["node_ref"] == consumer["node_ref"]
        and producer["node_ref"] is not None
    )
    item = {
        "producer_role": edge.producer_role,
        "consumer_role": edge.consumer_role,
        "source_endpoint_ref": src,
        "target_endpoint_ref": dst,
        "same_node": same_node,
        "same_physical_host": same_host,
        "bytes_per_transfer": edge.bytes_per_transfer,
        "transfer_count": edge.transfer_count,
        "requirement": edge.requirement,
        "link": {"status": status},
        "estimated_ms_per_transfer": None,
        "estimated_total_ms": None,
        "unit": "milliseconds (ESTIMATE)",
    }
    blocked, unknown, unmet = [], [], []
    if edge.same_node and not same_node:
        (blocked if edge.requirement == "hard" else unmet).append("SAME_NODE_UNMET")
    if found is not None:
        # Expired probes stay as provenance; only a measured link yields a cost.
        item["link"].update(
            bandwidth_bytes_per_second=found.bandwidth_bytes_per_second,
            request_overhead_ms=found.request_overhead_ms,
            measured_at=found.measured_at.isoformat(),
            evidence_ref=found.evidence_ref,
        )
    if status == "measured":
        per = found.request_overhead_ms + (
            edge.bytes_per_transfer / found.bandwidth_bytes_per_second * 1000
        )
        item["estimated_ms_per_transfer"] = _ms(per)
        item["estimated_total_ms"] = _ms(edge.transfer_count * per)
        if (
            edge.min_bandwidth_bytes_per_second is not None
            and found.bandwidth_bytes_per_second < edge.min_bandwidth_bytes_per_second
        ):
            (blocked if edge.requirement == "hard" else unmet).append("BANDWIDTH_BELOW_MINIMUM")
        if (
            edge.max_request_overhead_ms is not None
            and found.request_overhead_ms > edge.max_request_overhead_ms
        ):
            (blocked if edge.requirement == "hard" else unmet).append("OVERHEAD_ABOVE_MAXIMUM")
    else:
        reason = "LINK_" + status.upper()
        if edge.requirement == "hard" and (
            edge.min_bandwidth_bytes_per_second is not None
            or edge.max_request_overhead_ms is not None
            or not edge.same_node
        ):
            blocked.append("HARD_" + reason)
        else:
            unknown.append(reason)
    prefix = f"{edge.producer_role}->{edge.consumer_role}:"
    return (
        item,
        [prefix + r for r in blocked],
        [prefix + r for r in unknown],
        [prefix + r for r in unmet],
    )


def _match_devices(demands):
    """Deterministic bipartite matching of role device slots to distinct device IDs.

    Slots are tried in role order and devices in sorted order; a free device is
    preferred and an earlier role is moved (augmenting path) only when a later
    role would otherwise be stranded. Returns the pinned map and the roles left short.
    """
    slots = [(role, devices) for role, count, devices in demands for _ in range(count)]
    owner = {}

    def augment(i, seen):
        free = next((d for d in slots[i][1] if d not in owner), None)
        if free is not None:
            owner[free] = i
            return True
        for device in slots[i][1]:
            if device not in seen:
                seen.add(device)
                if augment(owner[device], seen):
                    owner[device] = i
                    return True
        return False

    short = []
    for i, (role, _) in enumerate(slots):
        if not augment(i, set()) and role not in short:
            short.append(role)
    pinned = {role: [] for role, _, _ in demands}
    for device, i in owner.items():
        pinned[slots[i][0]].append(device)
    return {role: sorted(devices) for role, devices in pinned.items()}, short


def _capacity(request, views, roles, identity_fresh=True):
    """Aggregate shared-host demand conservatively and pin distinct device IDs.

    Backend aliases of one physical host report independent headroom, so the
    combined CPU/memory demand must fit the smallest known view. Accelerator
    demand must fit the key's owners and any member that explicitly observed
    that key; a non-owner that does not report the key is ignored. Unknown
    blocks the claim.
    """
    blocked = []
    domains = {}
    for role in roles:
        v = views[role]
        key = v["physical_host_ref"] or f"{v['backend']}/{v['cluster_ref']}/{v['node_ref']}"
        domains.setdefault(key, []).append(v)
    for key, members in sorted(domains.items()):
        if len(members) < 2:
            continue
        need = Counter()
        for v in members:
            r = v["resources"]
            need["cpu"] += r["host_cpu"]
            need["memory"] += r["host_memory_mib"] * 2**20
            if r["accelerator_count"] and v["resource_key"]:
                need[v["resource_key"]] += r["accelerator_count"]
        for k, total in sorted(need.items()):
            seen = [
                v["headroom"].get(k)
                for v in members
                if k in ("cpu", "memory") or v["resource_key"] == k or k in v["headroom"]
            ]
            if any(h is not None and h < total for h in seen):
                blocked.append(f"{key}:SHARED_CAPACITY_EXCEEDED:{k}")
            elif any(h is None for h in seen):
                blocked.append(f"{key}:SHARED_CAPACITY_UNKNOWN:{k}")
    pinned = {}
    if request.require_distinct_physical_devices:
        demands = []
        for role in roles:
            v = views[role]
            count = v["resources"]["accelerator_count"]
            if not count:
                continue
            if v["allocation_mode"] == "virtual_slot":
                blocked.append(f"{role}:VIRTUAL_SLOT_NOT_PHYSICAL_DEVICE")
            elif v["device_refs"] is None:
                blocked.append(f"{role}:PHYSICAL_DEVICE_IDENTITY_UNKNOWN")
            elif not identity_fresh:
                blocked.append(f"{role}:PHYSICAL_DEVICE_IDENTITY_EXPIRED")
            else:
                demands.append((role, count, v["device_refs"]))
        pinned, short = _match_devices(demands)
        blocked += [f"{role}:DISTINCT_PHYSICAL_DEVICE_INSUFFICIENT" for role in short]
    return blocked, pinned


def plan_placement(snapshot, request, candidates, *, at=None, backend_order=None):
    at = at or now()
    backend_order = tuple(backend_order or ("kubernetes", "slurm"))
    if snapshot is not None and snapshot.ref != request.topology_snapshot_ref:
        raise ValueError("topology snapshot does not match the request")
    roles = [r.role_ref for r in request.roles]
    per_role = {r: [] for r in roles}
    spec = {r.role_ref: r for r in request.roles}
    for c in candidates:
        if c.role_ref not in per_role:
            raise ValueError("candidate references an unknown role")
        role = spec[c.role_ref]
        if c.workload_ref != role.workload_ref:
            raise ValueError(f"candidate {c.candidate_ref} workload_ref does not match role")
        if role.candidate_refs is not None and c.candidate_ref not in role.candidate_refs:
            raise ValueError(f"candidate {c.candidate_ref} is outside role candidate_refs")
        per_role[c.role_ref].append(c)
    for role, items in per_role.items():
        refs = [c.candidate_ref for c in items]
        if len(set(refs)) != len(refs):
            raise ValueError("duplicate candidate within role")
        if len(items) > MAX_CANDIDATES_PER_ROLE:
            raise ValueError(f"role {role} exceeds {MAX_CANDIDATES_PER_ROLE} candidates")
        items.sort(key=lambda c: c.candidate_ref)
    topo = snapshot_view(snapshot, at)
    expired = topo["status"] == "expired"
    endpoints = {}
    links = {}
    if snapshot is not None:
        endpoints = {(e.backend, e.cluster_ref, e.node_ref): e for e in snapshot.endpoints}
        links = {(x.source_endpoint_ref, x.target_endpoint_ref): x for x in snapshot.links}
    views = {
        role: [
            _candidate_view(
                c,
                endpoints.get((c.backend, c.cluster_ref, c.node_ref)),
                snapshot is not None,
            )
            for c in items
        ]
        for role, items in per_role.items()
    }
    eligible = {role: [v for v in items if v["eligible"]] for role, items in views.items()}
    total = math.prod(len(v) for v in eligible.values())
    if total > MAX_COMBINATIONS:
        raise ValueError(f"placement exceeds {MAX_COMBINATIONS} combinations")
    feasible, excluded = [], []
    for combo in itertools.product(*(eligible[r] for r in roles)):
        chosen = dict(zip(roles, combo, strict=True))
        blocked, pinned = _capacity(request, chosen, roles, identity_fresh=not expired)
        unknown = ["TOPOLOGY_SNAPSHOT_EXPIRED"] if expired else []
        unmet, transfers = [], []
        for edge in request.edges:
            item, b, u, s = _transfer(
                edge, chosen[edge.producer_role], chosen[edge.consumer_role], links, snapshot, at
            )
            transfers.append(item)
            blocked += b
            unknown += u
            unmet += s
        unknown += [
            f"{r}:AVAILABILITY_UNKNOWN" for r, v in chosen.items() if v["availability"] == "unknown"
        ]
        assignment = {r: v["candidate_ref"] for r, v in chosen.items()}
        if blocked:
            excluded.append({"assignment": assignment, "reasons": blocked})
            continue
        known = [t["estimated_total_ms"] for t in transfers if t["estimated_total_ms"] is not None]
        feasible.append(
            {
                "assignment": assignment,
                "status": "complete" if not unknown else "partially_unknown",
                "unknowns": unknown,
                "soft_unmet": unmet,
                "busy_roles": [r for r, v in chosen.items() if v["availability"] == "busy"],
                "pinned_device_refs": pinned,
                "transfers": transfers,
                "estimated_communication_ms": _ms(sum(known))
                if len(known) == len(transfers)
                else None,
                "known_partial_communication_ms": _ms(sum(known)),
                "unit": "milliseconds (ESTIMATE)",
            }
        )

    def order(item):
        cost = item["estimated_communication_ms"]
        return (
            bool(item["unknowns"]),
            len(item["soft_unmet"]),
            len(item["busy_roles"]),
            math.inf if cost is None else cost,
            tuple(
                backend_order.index(chosen["backend"]) if chosen["backend"] in backend_order else 99
                for chosen in (
                    next(v for v in eligible[r] if v["candidate_ref"] == item["assignment"][r])
                    for r in roles
                )
            ),
            tuple(item["assignment"][r] for r in roles),
        )

    feasible.sort(key=order)
    for rank, item in enumerate(feasible, 1):
        item["rank"] = rank
    reason_counts = Counter(
        r.split(":", 1)[1] if ":" in r else r for x in excluded for r in x["reasons"]
    )
    reasons = []
    for role in roles:
        if not eligible[role]:
            reasons.append(f"{role}:NO_ELIGIBLE_CANDIDATE")
    if not feasible and not reasons:
        reasons.append("NO_FEASIBLE_COMBINATION")
    selected = None
    if feasible:
        best = feasible[0]
        chosen_views = [
            next(v for v in eligible[r] if v["candidate_ref"] == best["assignment"][r])
            for r in roles
        ]
        selected = {
            "assignment": {
                r: {
                    k: v[k]
                    for k in (
                        "candidate_ref",
                        "workload_ref",
                        "backend",
                        "cluster_ref",
                        "node_ref",
                        "device_class",
                        "accelerator_model",
                        "resource_key",
                        "allocation_mode",
                        "endpoint_ref",
                        "physical_host_ref",
                        "device_refs",
                        "availability",
                    )
                }
                for r in roles
                for v in eligible[r]
                if v["candidate_ref"] == best["assignment"][r]
            },
            "status": best["status"],
            # A preview is never "runnable now": the runtime must recheck admission.
            "admission": "unknown"
            if any(v["availability"] == "unknown" for v in chosen_views)
            else "wait_for_resources"
            if best["busy_roles"]
            else "observed_headroom_recheck_at_submission",
            "pinned_device_refs": best["pinned_device_refs"],
            "unknowns": best["unknowns"],
            "soft_unmet": best["soft_unmet"],
            "transfers": best["transfers"],
            "estimated_communication_ms": best["estimated_communication_ms"],
            "unit": "milliseconds (ESTIMATE)",
        }
    plan = {
        "schema": "placement-plan/v1",
        "task_ref": request.task_ref,
        "evaluated_at": at.isoformat(),
        "accepted": selected is not None,
        "reasons": reasons,
        "request_digest": signature(request),
        "candidates_digest": signature(
            [c.model_dump(mode="json") for r in roles for c in per_role[r]]
        ),
        "snapshot": topo,
        "bounds": {
            "max_roles": MAX_ROLES,
            "max_candidates_per_role": MAX_CANDIDATES_PER_ROLE,
            "max_combinations": MAX_COMBINATIONS,
        },
        "candidates": [v for r in roles for v in views[r]],
        "combinations_evaluated": total,
        "combinations": feasible[:LISTED],
        "feasible_count": len(feasible),
        "excluded_combinations": {
            "count": len(excluded),
            "reason_counts": dict(sorted(reason_counts.items())),
            "items": excluded[:LISTED],
        },
        "selected": selected,
        "semantics": list(SEMANTICS),
    }
    plan["digest"] = signature(plan)
    return plan


def import_snapshot(service, project, snapshot):
    """Operator import; immutable per ref. Future observations are rejected."""
    from .service import Rejected

    if snapshot.observed_at > now() + FUTURE_SKEW:
        raise Rejected("topology snapshot observed_at is in the future")
    return service.register("topology_snapshot", snapshot, project)


def resolve_candidates(service, conn, project, request):
    """Registered candidates per role via the existing profile compiler.

    Callers cannot add capabilities: every reason from compile_plan (contract,
    profile, queue, Slurm training isolation, availability) is preserved, except
    that availability observed in another cluster's inventory becomes unknown.
    """
    from .scheduling import SchedulingPlanRequest, compile_plan
    from .service import required

    resolved = []
    for role in request.roles:
        spec = WorkloadSpec.model_validate(
            required(service.store, conn, "workload", role.workload_ref, project)
        )
        known = {c.ref: c for c in spec.candidates}
        refs = role.candidate_refs or tuple(sorted(known))
        if len(refs) > MAX_CANDIDATES_PER_ROLE:
            raise ValueError(f"role {role.role_ref} exceeds {MAX_CANDIDATES_PER_ROLE} candidates")
        if any(ref not in known for ref in refs):
            raise ValueError(f"role {role.role_ref} references an unregistered candidate")
        result = compile_plan(
            service,
            conn,
            project,
            SchedulingPlanRequest(
                profile_ref=request.scheduling_profile_ref, workload_ref=role.workload_ref
            ),
        )
        evaluation = result["plan"]["evaluation"] if result["accepted"] else result["evaluation"]
        evaluated = {item["candidate_ref"]: item for item in evaluation}
        for ref in refs:
            c = known[ref]
            cap = service.store.get(conn, "capability", c.capability_ref)
            cap = cap["body"] if cap and cap["project"] in {project, "*"} else {}
            live = evaluated.get(ref, {}).get("availability", {})
            reasons = tuple(result["excluded"].get(ref, ()))
            # Legacy observations are keyed by (backend, node_ref) only; accept one
            # solely from this project's inventory of the capability's own cluster.
            source = live.get("snapshot_ref") and service.store.get(
                conn, "inventory", live["snapshot_ref"]
            )
            if live.get("snapshot_ref") and not (
                source
                and source["project"] == project
                and source["body"].get("cluster_ref") == cap.get("backend_cluster_id")
                and source["body"].get("backend", "kubernetes") == str(c.backend)
            ):
                if live.get("status") == "unavailable":
                    reasons = tuple(r for r in reasons if r != live.get("reason"))
                live = {"status": "unknown", "headroom": {}}
            resolved.append(
                ResolvedCandidate(
                    role_ref=role.role_ref,
                    workload_ref=spec.ref,
                    candidate_ref=ref,
                    backend=c.backend,
                    cluster_ref=cap.get("backend_cluster_id"),
                    node_ref=cap.get("node_ref"),
                    device_class=cap.get("device_class"),
                    accelerator_model=cap.get("accelerator_model"),
                    resource_key=cap.get("resource_key"),
                    allocation_mode=c.context.allocation_mode,
                    resources=c.context.resources,
                    availability=live.get("status", "unknown"),
                    headroom=live.get("headroom", {}),
                    reasons=reasons,
                )
            )
    return resolved


def evaluate(service, conn, project, request, at=None):
    from .scheduling import SchedulingProfile
    from .service import required

    profile = SchedulingProfile.model_validate(
        required(service.store, conn, "scheduling_profile", request.scheduling_profile_ref, project)
    )
    snapshot = TopologySnapshot.model_validate(
        required(service.store, conn, "topology_snapshot", request.topology_snapshot_ref, project)
    )
    candidates = resolve_candidates(service, conn, project, request)
    return plan_placement(
        snapshot, request, candidates, at=at, backend_order=profile.policy.backend_order
    )
