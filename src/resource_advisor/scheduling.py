"""Versioned policy compilation above existing scheduler adapters.

Profiles reference administrator-provisioned queues/QOS. They neither rewrite
cluster configuration nor claim a quota shared across independent schedulers.
"""

from datetime import timedelta
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import func, or_, select

from .contracts import TERMINAL, Contract, Ref, Resources, WorkloadSpec, now, signature
from .inventory import fresh_view
from .policy import execution_compatibility
from .store import entities, jobs


class SchedulingPolicy(Contract):
    backend_order: tuple[Literal["kubernetes", "slurm"], ...] = ("kubernetes", "slurm")
    workload_types: tuple[Literal["training", "inference", "benchmark", "preprocessing"], ...] = (
        "training",
        "inference",
        "benchmark",
        "preprocessing",
    )
    allocation_modes: tuple[Literal["physical_device", "virtual_slot", "cpu_only"], ...] = (
        "physical_device",
        "virtual_slot",
    )
    priority: Literal["normal", "high"] = "normal"
    max_resources: Resources
    max_run_seconds: int = Field(ge=1, le=86400)
    max_queue_seconds: int = Field(ge=1, le=86400)
    quota_scope: Literal["backend", "global"] = "backend"
    preemption: Literal["inherit", "never", "allow"] = "inherit"

    @model_validator(mode="after")
    def orders_are_explicit(self):
        if not self.backend_order or len(set(self.backend_order)) != len(self.backend_order):
            raise ValueError("backend order must contain distinct backends")
        if not self.workload_types or not self.allocation_modes:
            raise ValueError("workload types and allocation modes are required")
        return self


class SchedulerBinding(Contract):
    backend: Literal["kubernetes", "slurm"]
    cluster_ref: Ref
    namespace: Ref | None = None
    local_queue: Ref | None = None
    runtime_class_name: Ref | None = None
    partition: Ref | None = None
    account: Ref | None = None
    priority_map: dict[Literal["normal", "high"], Ref | None]

    @model_validator(mode="after")
    def backend_fields(self):
        if self.backend == "kubernetes":
            if not self.namespace or not self.local_queue or self.partition or self.account:
                raise ValueError("Kubernetes binding requires namespace and LocalQueue")
        elif (
            not self.partition
            or not self.account
            or self.namespace
            or self.local_queue
            or self.runtime_class_name
        ):
            raise ValueError("Slurm binding requires partition and account")
        if self.backend == "slurm" and any(v is None for v in self.priority_map.values()):
            raise ValueError("Slurm priority mappings require a QOS")
        if self.priority_map.get("high", "absent") is None:
            raise ValueError("High priority requires an explicit backend mapping")
        return self

    def adapter(self, priority):
        if self.backend == "kubernetes":
            return {
                "namespace": self.namespace,
                "local_queue": self.local_queue,
                "runtime_class_name": self.runtime_class_name,
                "priority_class": self.priority_map[priority],
            }
        return {
            "partition": self.partition,
            "account": self.account,
            "qos": self.priority_map[priority],
        }


class SchedulingProfile(Contract):
    ref: Ref
    project_ref: Ref
    name: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    policy: SchedulingPolicy
    bindings: tuple[SchedulerBinding, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_bindings(self):
        keys = [(b.backend, b.cluster_ref) for b in self.bindings]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate scheduler binding")
        return self


class SchedulingPlanRequest(Contract):
    profile_ref: Ref
    workload_ref: Ref
    candidate_ref: Ref | None = None
    template_ref: Ref | None = None
    backend: Literal["auto", "kubernetes", "slurm"] = "auto"


def node_observations(conn, project):
    """Latest authorized inventory per cluster; missing telemetry stays unknown."""
    ranked = (
        select(
            entities.c.ref,
            func.row_number()
            .over(
                partition_by=entities.c.body["cluster_ref"].as_string(),
                order_by=(entities.c.created_at.desc(), entities.c.ref.desc()),
            )
            .label("rank"),
        )
        .where(entities.c.kind == "inventory", entities.c.project == project)
        .subquery()
    )
    rows = conn.execute(
        select(entities.c.body)
        .join(ranked, entities.c.ref == ranked.c.ref)
        .where(entities.c.kind == "inventory", entities.c.project == project, ranked.c.rank == 1)
    ).scalars()
    result = {}
    for raw in rows:
        snapshot = fresh_view(raw)
        for node in snapshot["nodes"]:
            key = (snapshot.get("backend", "kubernetes"), node["node_ref"])
            observation = {
                "snapshot_ref": snapshot["ref"],
                "collected_at": snapshot["collected_at"],
                "node": node,
            }
            if key not in result or observation["collected_at"] > result[key]["collected_at"]:
                result[key] = observation
    return result


def availability(candidate, cap, observations):
    observed = observations.get((str(candidate.backend), cap.node_ref))
    if observed is None:
        return {"status": "unknown", "reason": "NO_FRESH_INVENTORY", "headroom": {}}
    node = observed["node"]

    def value(signal):
        return signal.get("value") if signal and signal.get("status") == "ok" else None

    result = {
        "snapshot_ref": observed["snapshot_ref"],
        "collected_at": observed["collected_at"],
        "headroom": {},
    }
    states = value(node.get("scheduler_state")) or []
    if value(node.get("ready")) is False or any(
        x in str(states).upper() for x in ["DOWN", "DRAIN", "FAIL"]
    ):
        return {**result, "status": "unavailable", "reason": "OBSERVED_NODE_UNAVAILABLE"}
    resources = candidate.context.resources
    required = {"cpu": resources.host_cpu, "memory": resources.host_memory_mib * 2**20}
    if resources.accelerator_count:
        required[cap.resource_key] = resources.accelerator_count
    for key in required:
        result["headroom"][key] = value(
            node.get("resources", {}).get(key, {}).get("request_headroom")
        )
    if any(v is not None and v < required[k] for k, v in result["headroom"].items()):
        return {**result, "status": "busy", "reason": "WAIT_FOR_BACKEND_RESOURCES"}
    if any(v is None for v in result["headroom"].values()):
        return {**result, "status": "unknown", "reason": "NO_FRESH_INVENTORY"}
    return {**result, "status": "available", "reason": "OBSERVED_REQUEST_HEADROOM"}


def plan_digest(plan):
    # Inventory evidence may refresh without changing the actual execution plan.
    return signature({k: v for k, v in plan.items() if k != "evaluation"})


def compile_plan(service, conn, project, request):
    from .contracts import JobTemplate
    from .service import NotFound, Rejected, required

    profile = SchedulingProfile.model_validate(
        required(service.store, conn, "scheduling_profile", request.profile_ref, project)
    )
    if profile.project_ref != project:
        raise ValueError("profile belongs to another project")
    spec = WorkloadSpec.model_validate(
        required(service.store, conn, "workload", request.workload_ref, project)
    )
    if request.template_ref:
        template = JobTemplate.model_validate(
            required(service.store, conn, "job_template", request.template_ref, project)
        )
        if template.workload_ref != spec.ref or (
            template.candidate_ref is not None
            and request.candidate_ref
            not in {
                None,
                template.candidate_ref,
            }
        ):
            raise ValueError("template does not match the requested workload/candidate")
        if template.candidate_ref is not None:
            request = request.model_copy(update={"candidate_ref": template.candidate_ref})
        spec = service.template_spec(spec, template)
    policy = profile.policy
    common = []
    if policy.quota_scope != "backend":
        common.append("GLOBAL_QUOTA_UNSUPPORTED")
    if policy.preemption != "inherit":
        common.append("PREEMPTION_OVERRIDE_UNSUPPORTED")
    if spec.identity.task_type not in policy.workload_types:
        common.append("WORKLOAD_TYPE_DENIED")
    candidates = [c for c in spec.candidates if request.candidate_ref in {None, c.ref}]
    if not candidates:
        common.append("CANDIDATE_NOT_FOUND")
    excluded = {}
    eligible = []
    evaluation = []
    observations = node_observations(conn, project)
    # Native inventory can lag a simultaneous burst. This is project-local
    # accepted demand, not an invented physical reservation or a global quota.
    demand = {}
    recent = {}
    cutoff = (now() - timedelta(minutes=5)).isoformat()
    for body, state in conn.execute(
        select(jobs.c.body, jobs.c.state).where(
            jobs.c.project == project,
            or_(~jobs.c.state.in_(TERMINAL), jobs.c.body["created_at"].as_string() >= cutoff),
        )
    ):
        capability = body.get("capability", {})
        key = (body.get("candidate", {}).get("backend"), capability.get("node_ref"))
        if state not in TERMINAL:
            demand[key] = demand.get(key, 0) + 1
        # Short jobs can finish before all burst requests are accepted. Retain
        # their recent assignment as a tie breaker, never as reserved capacity.
        if body.get("created_at", "") >= cutoff:
            recent[key] = recent.get(key, 0) + 1
    for candidate in candidates:
        errors = list(common)
        try:
            _, _, variant, cap = service.bundle(conn, project, spec.ref, candidate.ref)
        except (NotFound, Rejected):
            excluded[candidate.ref] = errors + ["CANDIDATE_CONTRACT_UNAVAILABLE"]
            continue
        errors += execution_compatibility(
            spec, candidate, variant, cap, operational=service.operational_mode
        )
        if not service.operational_mode and candidate.ref != spec.baseline_candidate_ref:
            errors.append("APPROVAL_REQUIRED")
        if candidate.backend not in policy.backend_order or request.backend not in {
            "auto",
            candidate.backend,
        }:
            errors.append("BACKEND_DENIED")
        if candidate.context.allocation_mode not in policy.allocation_modes:
            errors.append("ALLOCATION_MODE_DENIED")
        if any(
            getattr(candidate.context.resources, k) > getattr(policy.max_resources, k)
            for k in ["host_cpu", "host_memory_mib", "accelerator_count"]
        ):
            errors.append("PROFILE_RESOURCE_LIMIT")
        if candidate.backend == "slurm" and spec.identity.task_type == "training":
            errors.append("SLURM_TRAINING_ISOLATION_UNSUPPORTED")
        binding = next(
            (
                b
                for b in profile.bindings
                if b.backend == candidate.backend and b.cluster_ref == cap.backend_cluster_id
            ),
            None,
        )
        if binding is None:
            errors.append("SCHEDULER_BINDING_MISSING")
        elif policy.priority not in binding.priority_map:
            errors.append("PRIORITY_UNSUPPORTED")
        live = availability(candidate, cap, observations)
        key = (str(candidate.backend), cap.node_ref)
        live["accepted_active_jobs"] = demand.get(key, 0)
        live["accepted_recent_jobs"] = recent.get(key, 0)
        live["recent_assignment_window_seconds"] = 300
        if live["status"] == "unavailable":
            errors.append(live["reason"])
        evaluation.append(
            {
                "candidate_ref": candidate.ref,
                "node_ref": cap.node_ref,
                "backend": str(candidate.backend),
                "model": cap.accelerator_model,
                "availability": live,
                "resources": candidate.context.resources.model_dump(mode="json"),
                "eligible": not errors,
                "reasons": list(dict.fromkeys(errors)),
            }
        )
        if errors:
            excluded[candidate.ref] = list(dict.fromkeys(errors))
        else:
            eligible.append((candidate, cap, binding, live))
    if not eligible:
        return {
            "accepted": False,
            "reasons": common or ["NO_ELIGIBLE_CANDIDATE"],
            "excluded": excluded,
            "plan": None,
            "digest": None,
            "evaluation": evaluation,
        }
    ranks = {"available": 0, "busy": 1, "unknown": 2}
    eligible.sort(
        key=lambda x: (
            ranks[x[3]["status"]],
            policy.backend_order.index(x[0].backend),
            x[3]["accepted_active_jobs"],
            x[3]["accepted_recent_jobs"],
            x[0].ref,
        )
    )
    candidate, cap, binding, live = eligible[0]
    execution = spec.execution.model_dump(mode="json")
    execution.update(
        priority=policy.priority,
        max_run_seconds=min(execution["max_run_seconds"], policy.max_run_seconds),
        max_queue_seconds=min(execution["max_queue_seconds"], policy.max_queue_seconds),
    )
    plan = {
        "profile_ref": profile.ref,
        "profile_version": profile.version,
        "profile_digest": signature(profile),
        "workload_ref": spec.ref,
        "workload_digest": signature(spec),
        "candidate_ref": candidate.ref,
        "backend": str(candidate.backend),
        "cluster_ref": cap.backend_cluster_id,
        "node_ref": cap.node_ref,
        "resource_key": cap.resource_key,
        "allocation_mode": cap.allocation_mode,
        "resources": candidate.context.resources.model_dump(mode="json"),
        "execution": execution,
        "adapter": binding.adapter(policy.priority),
        "quota_scope": "backend",
        "preemption": "inherit",
        "evaluation": [
            {**item, "selected": item["candidate_ref"] == candidate.ref} for item in evaluation
        ],
        "reason": [
            "Verified workload candidate satisfies profile constraints",
            "Fresh headroom first, then backend preference, active accepted demand, five-minute assignment history and candidate reference",
            "Backend queue/account enforces its own quota; no cross-backend quota is reserved",
        ],
    }
    return {
        "accepted": True,
        "reasons": [],
        "excluded": excluded,
        "plan": plan,
        "digest": plan_digest(plan),
    }


def apply_plan(spec, plan):
    body = spec.model_dump(mode="json")
    body["execution"] = plan["execution"]
    return WorkloadSpec.model_validate(body)


def verify_adapter_plan(job, actual):
    """Prevent route drift from silently changing a previously accepted policy."""
    plan = job["body"].get("scheduling_plan")
    if plan is not None and plan["adapter"] != actual:
        raise ValueError("SCHEDULING_BINDING_DRIFT")
