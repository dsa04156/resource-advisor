"""Model-first training catalog: a read-only projection, never an execution authority.

Models are named training workloads (``task_type=training``, ``model_name``) with at
least one TRAINING_VERIFIED candidate. Workloads that share an exact training
condition are grouped but keep their own identity signatures, profiles and runs.
Resource combinations are the registered candidates; estimates come only from the
existing scheduling-plan preview under an existing recommendation profile, so an
unmeasured combination stays unknown instead of being scaled from another one.
"""

from .contracts import WorkloadSpec, now, signature
from .jct_report import (
    DEFAULT_QUEUE_AGE,
    _node_observation,
    _readiness,
    _recent_runs,
    _run_counts,
)

TEACHER_KEYS = (
    "schema",
    "role",
    "device_class",
    "device_name",
    "accelerator_model",
    "model_sha256",
    "model_file",
    "runtime",
    "runtime_version",
    "target_cores",
    "node_name",
    "pod_uid",
    "reservation",
    "calls",
    "request_bytes",
    "response_bytes",
    "client_rpc_seconds",
    "server_infer_seconds",
    "server_handler_seconds",
    "objective",
)

# Training objectives this repository's runtimes actually execute, by signed boundary.
OBJECTIVES = {
    "vision-adamw-fixed-steps-synchronized-v1": "gpu-supervised",
    "vision-kd-mobilint-teacher-fixed-steps-v1": "gpu-kd-mobilint-teacher",
}
TEACHER_POD_FLAG = "--teacher-pod-uid"


def teacher_pin(command):
    """The teacher Pod UID a signed joint runtime command pins, if any."""
    for i, arg in enumerate(command or ()):
        if arg == TEACHER_POD_FLAG and i + 1 < len(command):
            return command[i + 1]
        if str(arg).startswith(TEACHER_POD_FLAG + "="):
            return str(arg).split("=", 1)[1]
    return None


POLICY_ORDER = {"profile_only": 0, "profile_queue": 1, "profile_queue_network": 2}
ACCELERATOR_CLASSES = {"gpu", "npu", "unknown"}


def model_key(identity):
    return signature({"model_name": identity.model_name, "model_digest": identity.model_digest})[
        7:23
    ]


def condition_key(identity):
    """Grouping key: a fixed global batch groups allocations whose local batch differs."""
    c = condition(identity)
    if c["global_batch_size"] is not None:
        c.pop("batch_size")
        c.pop("gradient_accumulation")
        if c["sample_shape"] is not None:
            # Axis 0 is the declared per-rank micro batch; it varies with the allocation.
            c.pop("input_shape")
    return signature(c)


def condition(identity):
    """The exact registered training condition; nothing here is user-editable."""
    i = identity
    return {
        "dataset_version": i.dataset_version,
        "input_shape": list(i.input_shape),
        # Training identities declare input_shape[0] as the per-rank micro batch.
        "sample_shape": list(i.input_shape[1:])
        if len(i.input_shape) > 1 and i.input_shape[0] == i.batch_size
        else None,
        "config_digest": i.config_digest,
        "batch_size": i.batch_size,
        "global_batch_size": i.global_batch_size,
        "gradient_accumulation": i.gradient_accumulation,
        "precision": i.precision,
        "optimizer": i.optimizer,
        "optimizer_parameters": dict(i.optimizer_parameters),
        "steps": i.work_units,
        "seed": i.seed,
        "measurement_boundary": i.measurement_boundary,
    }


def _auxiliary(service, conn, project, runs):
    """Attach the NPU teacher attested in each run's own training receipt.

    The receipt is persisted on native completion; only whitelisted summary keys
    are projected. The teacher is a pre-reserved auxiliary Pod, never presented
    as part of the GPU job's scheduler reservation.
    """
    from sqlalchemy import select

    from .store import jobs

    for run in runs:
        run["auxiliary_devices"] = []
        attempt = conn.execute(
            select(jobs.c.body["attempt_id"].as_string()).where(
                jobs.c.project == project, jobs.c.id == run["job_id"]
            )
        ).scalar()
        receipt = service.store.get(conn, "training_receipt", attempt) if attempt else None
        if not receipt or receipt["project"] != project:
            continue
        teacher = (receipt["body"].get("output_checkpoint") or {}).get("teacher")
        if isinstance(teacher, dict):
            run["auxiliary_devices"].append(
                {
                    **{k: teacher.get(k) for k in TEACHER_KEYS},
                    "source": "training_receipt.output_checkpoint.teacher",
                    "reservation_kind": (
                        "pre-reserved auxiliary Pod; not a joint scheduler reservation"
                    ),
                }
            )
    return runs


def _estimate_profile(store, conn, project, profile_ref, targets=()):
    """An explicit choice wins; otherwise prefer profiles whose scheduler bindings
    cover every (backend, cluster) of the selected candidates, then policy order."""
    profiles = [
        x["body"]
        for x in store.list(conn, "scheduling_profile", project)
        if x["body"]["policy"].get("recommendation")
    ]
    if profile_ref:
        return next((p for p in profiles if p["ref"] == profile_ref), None), profiles

    def binds(p):
        bound = {(b["backend"], b["cluster_ref"]) for b in p.get("bindings", [])}
        return all(t in bound for t in targets)

    ranked = sorted(
        profiles,
        key=lambda p: (
            binds(p),
            POLICY_ORDER[p["policy"]["recommendation"]["policy"]],
            p["ref"],
        ),
    )
    return (ranked[-1] if ranked else None), profiles


def _preview(service, conn, project, profile, workload_ref):
    """The existing scheduling-plan preview; nothing is reserved or submitted."""
    from .scheduling import SchedulingPlanRequest, compile_plan
    from .service import NotFound, Rejected

    try:
        result = compile_plan(
            service,
            conn,
            project,
            SchedulingPlanRequest(profile_ref=profile["ref"], workload_ref=workload_ref),
        )
    except (ValueError, NotFound, Rejected) as exc:
        return None, str(exc)
    return result, None


def _jct(profile, preview, error, evaluation, prediction):
    if profile is None:
        return {"status": "NO_ESTIMATE_POLICY", "predicted_seconds": None, "reasons": []}
    if error:
        return {"status": "ESTIMATE_ERROR", "predicted_seconds": None, "reasons": [error[:300]]}
    if evaluation is not None and not evaluation.get("eligible"):
        return {
            "status": "EXCLUDED_BY_POLICY",
            "predicted_seconds": None,
            "reasons": evaluation.get("reasons", []),
        }
    if prediction and prediction.get("predicted_jct_seconds") is not None:
        return {
            "status": "ESTIMATED",
            "predicted_seconds": prediction["predicted_jct_seconds"],
            "reasons": prediction.get("reasons", []),
            "breakdown": prediction.get("breakdown"),
            "source": "existing recommendation preview",
            "interval": "not calibrated; observed spread only (breakdown.uncertainty)",
        }
    reasons = (prediction or {}).get("reasons", [])
    pilot = not reasons or all(r.startswith("PROFILE_") for r in reasons)
    return {
        "status": "PILOT_REQUIRED" if pilot else "UNKNOWN",
        "predicted_seconds": None,
        "reasons": reasons,
    }


def _combination(
    service, conn, project, spec, row, *, at, inventory, queue_age, profile, preview, error, runs
):
    candidate = next(c for c in spec.candidates if c.ref == row["candidate_ref"])
    context = candidate.context
    params = dict(context.parameters)
    count = context.resources.accelerator_count
    plan = (preview or {}).get("plan") or {}
    evaluation = {
        e["candidate_ref"]: e for e in plan.get("evaluation", (preview or {}).get("evaluation", []))
    }.get(candidate.ref)
    evidence = (
        plan.get("recommendation_evidence") or (preview or {}).get("recommendation_evidence") or {}
    )
    prediction = next(
        (p for p in evidence.get("predictions", []) if p["candidate_ref"] == candidate.ref), None
    )
    distributed = getattr(context, "distributed", None)
    strategy = distributed.strategy if distributed else "single"
    verified = row.get("verification") == "TRAINING_VERIFIED"
    blockers = list(row["submission_reasons"])
    variant = service.store.get(conn, "variant", row.get("variant_ref"))
    pinned = teacher_pin(((variant or {}).get("body") or {}).get("command"))
    if pinned:
        # A pre-reserved auxiliary teacher cannot be proven current from here; only a
        # dedicated provisioning flow may run it again. Past receipts stay visible.
        blockers.append("AUXILIARY_RESERVATION_REQUIRED")
    if not verified:
        blockers.append("TRAINING_NOT_VERIFIED")
    if strategy != "single" and not verified:
        blockers.append("DISTRIBUTED_STRATEGY_NOT_VALIDATED")
    readiness = (
        _readiness(service.store, conn, project, spec, row, at, queue_age)
        if row.get("node_ref")
        else None
    )
    measured_peak = next(
        (
            (r["result"] or {}).get("peak_memory_mib")
            for r in runs
            if r["candidate_ref"] == candidate.ref and r["result"]
        ),
        None,
    )
    return {
        "workload_ref": spec.ref,
        "identity_signature": signature(spec.identity),
        "candidate_ref": candidate.ref,
        "backend": str(candidate.backend),
        "model": row.get("model"),
        "device_class": row.get("device_class"),
        "strategy": strategy,
        "distributed": None
        if distributed is None
        else {
            "launcher": distributed.launcher,
            "collective": distributed.collective,
            "placement": distributed.placement,
            "nproc_per_node": distributed.nproc_per_node,
            "world_size": distributed.world_size,
        },
        "declared_parameters": params,
        "local_batch_size": spec.identity.batch_size,
        "gradient_accumulation": spec.identity.gradient_accumulation,
        # A time-shared slot still runs on one physical GPU, never one GPU per slot.
        "physical_devices": distributed.world_size
        if distributed
        else count
        if context.allocation_mode == "physical_device"
        else (1 if count else None)
        if context.allocation_mode == "virtual_slot"
        else 0,
        "exclusive": context.allocation_mode == "physical_device",
        "shared_slots": count if context.allocation_mode == "virtual_slot" else None,
        "allocation_mode": context.allocation_mode,
        # Every allocated member as declared: mixed allocations keep each node's own model.
        "members": [
            {
                "node_ref": n.node_ref,
                "accelerator_model": n.accelerator_model,
                "gpus": n.gpus,
                "capability_ref": n.capability_ref,
                "per_rank_batch_size": spec.identity.batch_size,
            }
            for n in distributed.nodes
        ]
        if distributed
        else [
            {
                "node_ref": row.get("node_ref"),
                "accelerator_model": row.get("model"),
                "gpus": count
                if context.allocation_mode == "physical_device"
                else (1 if count else 0),
                "capability_ref": candidate.capability_ref,
                "per_rank_batch_size": spec.identity.batch_size,
            }
        ],
        "node_refs": [n.node_ref for n in distributed.nodes]
        if distributed
        else [row["node_ref"]]
        if row.get("node_ref")
        else [],
        "memory": {
            "aggregation": "per device; never summed across devices",
            "semantics": distributed.memory_semantics if distributed else "single replica",
            "memory_model": context.memory_model,
            "required_peak_mib_per_device": spec.quality.maximum_peak_memory_mib,
            "measured_peak_mib_per_device": measured_peak,
            "host_memory_mib": context.resources.host_memory_mib,
        },
        "verification": row.get("verification"),
        "submittable_now": row["submittable_now"] and not pinned,
        "auxiliary_reservation": None
        if not pinned
        else {
            "resource": "mobilint.com/npu",
            "pinned_pod_uid": pinned,
            "status": "unproven: requires a dedicated provisioning flow",
        },
        "blockers": blockers,
        "warnings": row.get("submission_warnings", []),
        "availability": (evaluation or {}).get("availability"),
        "node_observation": _node_observation(inventory, row, at) if row.get("node_ref") else None,
        "readiness": readiness,
        "evidence": "MEASURED"
        if readiness and readiness["jct_profiles"]["fresh"] > 0
        else "UNMEASURED",
        "jct": _jct(profile, preview, error, evaluation, prediction),
    }


def _coverage(inventory, trainable, at):
    rows = []
    for s in inventory:
        for n in s["nodes"]:
            ready = n.get("ready") or n.get("scheduler_state") or {}
            for key, r in (n.get("resources") or {}).items():
                device_class = r.get("device_class")
                # Only vendor extended resources can be accelerators (not cpu/memory).
                if "/" not in key or device_class not in ACCELERATOR_CLASSES:
                    continue
                capacity = (r.get("capacity") or {}).get("value")
                mode = r.get("allocation_mode")
                qualified = (n["node_ref"], key) in trainable
                reason = (
                    None
                    if qualified
                    else "NPU_NO_TRAINING_RUNTIME"
                    if device_class == "npu"
                    else "DEVICE_CLASS_UNREPORTED"
                    if device_class == "unknown"
                    else "NO_TRAINING_VARIANT"
                )
                rows.append(
                    {
                        "cluster_ref": s["cluster_ref"],
                        "snapshot_status": s["status"],
                        "collected_at": s["collected_at"],
                        "node_ref": n["node_ref"],
                        "resource_key": key,
                        "device_class": device_class,
                        "allocation_mode": mode,
                        "physical_devices": capacity if mode == "physical_device" else None,
                        "shared_slots": capacity if mode == "virtual_slot" else None,
                        "requested": (r.get("requested") or {}).get("value"),
                        "node_ready": ready.get("value") if ready.get("status") == "ok" else None,
                        "training_qualified": qualified,
                        "training_reason": reason,
                    }
                )
    return rows


def training_catalog(service, project, model=None, config=None, profile_ref=None):
    from .console import candidate_rows
    from .inventory import latest_inventory_views
    from .service import NotFound

    store, at = service.store, now()
    with store.transaction() as conn:
        specs = [
            WorkloadSpec.model_validate(x["body"])
            for x in store.list(conn, "workload", project)
            if x["body"]["identity"]["task_type"] == "training"
        ]
        rows = {s.ref: candidate_rows(service, conn, project, s) for s in specs}
        runs = _run_counts(conn, project)
        inventory = latest_inventory_views(conn, project, at=at)
        models, unqualified, trainable = {}, [], set()
        for s in specs:
            verified = [c for c in rows[s.ref] if c.get("verification") == "TRAINING_VERIFIED"]
            reasons = []
            if not s.identity.model_name:
                reasons.append("MODEL_NAME_MISSING")
            if not verified:
                reasons.append("NO_TRAINING_VERIFIED_CANDIDATE")
            counts = runs.get(s.ref, {"total": 0, "succeeded": 0})
            if reasons:
                unqualified.append(
                    {
                        "workload_ref": s.ref,
                        "model_name": s.identity.model_name,
                        "reasons": reasons,
                        "verification": sorted({c.get("verification") or "?" for c in rows[s.ref]}),
                        "candidate_reasons": sorted(
                            {r for c in rows[s.ref] for r in c.get("reasons", [])}
                        ),
                        "strategies": sorted(
                            {
                                getattr(getattr(c.context, "distributed", None), "strategy", None)
                                or "single"
                                for c in s.candidates
                            }
                        ),
                        "runs": counts,
                    }
                )
                if not s.identity.model_name:
                    continue
            trainable |= {(c.get("node_ref"), c.get("resource_key")) for c in verified}
            key = model_key(s.identity)
            m = models.setdefault(
                key,
                {
                    "model_key": key,
                    "model_name": s.identity.model_name,
                    "model_digest": s.identity.model_digest,
                    "workloads": [],
                    "qualified": False,
                    "conditions": set(),
                    "runs": {"total": 0, "succeeded": 0},
                },
            )
            m["workloads"].append(s)
            m["qualified"] = m["qualified"] or not reasons
            m["conditions"].add(condition_key(s.identity))
            m["runs"]["total"] += counts["total"]
            m["runs"]["succeeded"] += counts["succeeded"]
        listing = [
            {
                "model_key": m["model_key"],
                "model_name": m["model_name"],
                "model_digest": m["model_digest"],
                "qualified": m["qualified"],
                "workload_count": len(m["workloads"]),
                "condition_count": len(m["conditions"]),
                "devices": sorted(
                    {c["model"] for s in m["workloads"] for c in rows[s.ref] if c.get("model")}
                ),
                "runs": m["runs"],
            }
            for m in sorted(models.values(), key=lambda m: (not m["qualified"], m["model_name"]))
        ]
        result = {
            "project_ref": project,
            "generated_at": at.isoformat(),
            "semantics": (
                "Models are named training workloads; a model without a TRAINING_VERIFIED "
                "candidate is listed with qualified=false and cannot run. Conditions are "
                "registered and not editable. Combinations are registered candidates; JCT comes "
                "only from the existing recommendation preview and stays unknown when "
                "unmeasured. Inventory coverage is not a runnable combination."
            ),
            "models": listing,
            "unqualified": unqualified,
            "inventory": _coverage(inventory, trainable, at),
            "selected": None,
        }
        if model is None:
            return result
        if model not in models:
            raise NotFound("training model not found")
        groups = {}
        for s in models[model]["workloads"]:
            groups.setdefault(condition_key(s.identity), []).append(s)
        ordered = sorted(
            groups.items(), key=lambda kv: (condition(kv[1][0].identity)["steps"], kv[0])
        )
        # Default: the condition with the most successful native runs, so the measured
        # condition opens first; existing order breaks ties. Explicit choice wins.
        succeeded = {
            k: sum(runs.get(s.ref, {}).get("succeeded", 0) for s in members)
            for k, members in ordered
        }
        chosen_key = (
            config
            if config in groups
            else min(enumerate(ordered), key=lambda x: (-succeeded[x[1][0]], x[0]))[1][0]
        )
        targets = {
            (str(c["backend"]), c.get("cluster_ref"))
            for s in groups[chosen_key]
            for c in rows[s.ref]
        }
        profile, profiles = _estimate_profile(store, conn, project, profile_ref, targets)
        queue_age = min(
            (
                p["policy"]["recommendation"].get("max_queue_age_seconds", DEFAULT_QUEUE_AGE)
                for p in profiles
            ),
            default=DEFAULT_QUEUE_AGE,
        )
        configs, combinations = [], []
        for key, members in ordered:
            workloads = []
            for s in sorted(members, key=lambda s: s.ref):
                recent = (
                    _auxiliary(service, conn, project, _recent_runs(service, conn, project, s.ref))
                    if key == chosen_key
                    else []
                )
                workloads.append(
                    {
                        "workload_ref": s.ref,
                        "identity_signature": signature(s.identity),
                        "runs": runs.get(s.ref, {"total": 0, "succeeded": 0}),
                        "recent_runs": recent,
                    }
                )
                if key != chosen_key:
                    continue
                preview, error = (
                    _preview(service, conn, project, profile, s.ref) if profile else (None, None)
                )
                combinations += [
                    _combination(
                        service,
                        conn,
                        project,
                        s,
                        row,
                        at=at,
                        inventory=inventory,
                        queue_age=queue_age,
                        profile=profile,
                        preview=preview,
                        error=error,
                        runs=recent,
                    )
                    for row in rows[s.ref]
                ]
            configs.append(
                {
                    "config_key": key,
                    "condition": condition(members[0].identity),
                    "objective": OBJECTIVES.get(members[0].identity.measurement_boundary),
                    "workloads": workloads,
                }
            )
        result["selected"] = {
            "model": next(m for m in listing if m["model_key"] == model),
            "config_key": chosen_key,
            "configs": configs,
            "estimate_profile": None
            if profile is None
            else {
                "ref": profile["ref"],
                "policy": profile["policy"]["recommendation"]["policy"],
                "version": profile["policy"]["recommendation"].get("version"),
            },
            "estimate_profiles": [p["ref"] for p in profiles],
            "combinations": combinations,
        }
    return result
