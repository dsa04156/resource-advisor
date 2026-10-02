"""Operator-approved transfer families and immutable, database-derived source data."""

import math
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, ExecutionResult, Ref, now, signature
from .policy import compatibility, context_signature
from .rgpe import RGPEInput, TransferObservation, TransferTask
from .service import NotFound, Rejected, required


class TransferSpace(Contract):
    ref: Ref
    project_ref: Ref
    target_workload_ref: Ref
    source_workload_refs: tuple[Ref, ...] = Field(min_length=1, max_length=4)
    feature_names: tuple[Literal["host_cpu", "host_memory_mib"], ...] = Field(
        min_length=1, max_length=2
    )
    maximum_input_elements_ratio: float = Field(default=4, ge=1, le=16)
    maximum_source_age_seconds: int = Field(default=3600, ge=1, le=86400)
    rank_tie_fraction: float = Field(default=0.05, ge=0, le=0.25)
    maximum_source_rank_loss: float = Field(default=0.25, ge=0, le=0.5)

    @model_validator(mode="after")
    def distinct_references(self):
        refs = (self.target_workload_ref, *self.source_workload_refs)
        if len(set(refs)) != len(refs) or len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("distinct workloads and features required")
        return self


class TransferEvidenceRequest(Contract):
    ref: Ref
    job_ids: tuple[Ref, ...] = Field(min_length=6, max_length=512)

    @model_validator(mode="after")
    def distinct_jobs(self):
        if len(set(self.job_ids)) != len(self.job_ids):
            raise ValueError("source Jobs must be unique")
        return self


class TransferSpaces:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def create(self, project, request: TransferSpace):
        request = TransferSpace.model_validate(request.model_dump(mode="json"))
        if request.project_ref != project:
            raise Rejected("transfer space project differs from principal")
        with self.store.transaction() as conn:
            previous = self.store.get(conn, "transfer_space", request.ref)
            if previous:
                if previous["project"] != project or previous["body"][
                    "request"
                ] != request.model_dump(mode="json"):
                    raise Rejected("immutable transfer space already exists")
                return previous["body"]
            target = required(self.store, conn, "workload", request.target_workload_ref, project)
            targets = {c["ref"]: c for c in target["candidates"]}
            if not 3 <= len(targets) <= 12:
                raise Rejected("transfer requires three to twelve configurations")
            bounds = {}
            for name in request.feature_names:
                values = [c["context"]["resources"][name] for c in targets.values()]
                lo, hi = min(values), max(values)
                if lo == hi:
                    raise Rejected("each declared transfer feature must vary")
                bounds[name] = [lo, hi]
            options = [
                {
                    "candidate_ref": ref,
                    "coordinates": [
                        (c["context"]["resources"][name] - bounds[name][0])
                        / (bounds[name][1] - bounds[name][0])
                        for name in request.feature_names
                    ],
                }
                for ref, c in sorted(targets.items())
            ]
            if len({tuple(o["coordinates"]) for o in options}) != len(targets):
                raise Rejected("numeric coordinates must distinguish all configurations")
            bindings, task_signatures, common = [], set(), None
            for workload_ref in (request.target_workload_ref, *request.source_workload_refs):
                raw = required(self.store, conn, "workload", workload_ref, project)
                if {c["ref"] for c in raw["candidates"]} != set(targets):
                    raise Rejected("source and target configuration refs must match")
                if raw["baseline_candidate_ref"] != target["baseline_candidate_ref"]:
                    raise Rejected("transfer must preserve the baseline")
                identity = dict(raw["identity"])
                # Alias refs, seeds or repetition counts are not independent tasks.
                semantic = {k: v for k, v in identity.items() if k not in {"seed", "work_units"}}
                if signature(semantic) in task_signatures:
                    raise Rejected("source tasks must differ beyond alias, seed or work units")
                task_signatures.add(signature(semantic))
                shape, target_shape = identity["input_shape"], target["identity"]["input_shape"]
                ratio = math.prod(shape) / math.prod(target_shape)
                if len(shape) != len(target_shape) or not (
                    1 / request.maximum_input_elements_ratio
                    <= ratio
                    <= request.maximum_input_elements_ratio
                ):
                    raise Rejected("input shape is outside the approved transfer envelope")
                for candidate_ref in sorted(targets):
                    spec, candidate, variant, cap = self.service.bundle(
                        conn, project, workload_ref, candidate_ref
                    )
                    errors = compatibility(spec, candidate, variant, cap)
                    if workload_ref != request.target_workload_ref:
                        errors = [e for e in errors if e != "CAPABILITY_STALE"]
                    if errors:
                        raise Rejected("transfer compatibility: " + ",".join(errors))
                    if (
                        spec.identity.task_type not in {"inference", "benchmark"}
                        or spec.identity.sampling_policy_digest
                    ):
                        raise Rejected("training and sampled-workload transfer are not qualified")
                    if (
                        not spec.profiling.consent
                        or variant.device_class != "gpu"
                        or candidate.context.allocation_mode != "physical_device"
                    ):
                        raise Rejected("transfer requires consent and a qualified physical GPU")
                    if not variant.pilot_command or variant.pilot_command != variant.command:
                        raise Rejected("source and target require identical bounded entrypoints")
                    if (
                        candidate.context.model_dump(mode="json")
                        != targets[candidate_ref]["context"]
                    ):
                        raise Rejected(
                            "each transfer configuration must retain its target resources"
                        )
                    context = candidate.context.model_dump(mode="json")
                    for name in request.feature_names:
                        context["resources"].pop(name)
                    invariant = {
                        "identity": {
                            k: v
                            for k, v in identity.items()
                            if k not in {"input_shape", "dataset_version", "seed"}
                        },
                        "quality": spec.quality.model_dump(mode="json"),
                        "context": context,
                        "backend": candidate.backend,
                        "cluster": cap.backend_cluster_id,
                        "node": cap.node_ref,
                        "image": variant.image,
                        "command": variant.command,
                        "compiled_artifact": variant.compiled_artifact_digest,
                        "vendor": variant.accelerator_vendor,
                        "thermal_policy": variant.thermal_policy.model_dump(mode="json")
                        if variant.thermal_policy
                        else None,
                    }
                    current = signature(invariant)
                    if common is not None and current != common:
                        raise Rejected("unqualified runtime, model, code or workload-family change")
                    common = current
                    bindings.append(
                        {
                            "workload_ref": workload_ref,
                            "candidate_ref": candidate_ref,
                            "workload_digest": signature(spec),
                            "workload_signature": signature(spec.identity),
                            "variant_digest": signature(variant),
                            "capability_digest": signature(cap),
                            "context_signature": context_signature(candidate, variant),
                        }
                    )
            body = {
                "request": request.model_dump(mode="json"),
                "registered_at": now().isoformat(),
                "runtime_group_signature": common,
                "quality": target["quality"],
                "feature_bounds": bounds,
                "options": options,
                "bindings": bindings,
                "execution_authorized": False,
                "status": "SOURCE_EVIDENCE_AND_TARGET_CHECKS_REQUIRED",
            }
            return self.store.put(conn, "transfer_space", request.ref, project, body)["body"]

    def get(self, project, ref):
        with self.store.transaction() as conn:
            return required(self.store, conn, "transfer_space", ref, project)

    def _bindings(self, conn, project, space):
        for binding in space["bindings"]:
            spec, candidate, variant, cap = self.service.bundle(
                conn, project, binding["workload_ref"], binding["candidate_ref"]
            )
            if (
                signature(spec) != binding["workload_digest"]
                or signature(variant) != binding["variant_digest"]
                or signature(cap) != binding["capability_digest"]
            ):
                raise Rejected("immutable transfer binding changed")
            if binding["workload_ref"] == space["request"]["target_workload_ref"] and compatibility(
                spec, candidate, variant, cap
            ):
                raise Rejected("target execution context is no longer compatible")

    def _source_records(self, conn, project, space, job_ids):
        tasks = {ref: [] for ref in space["request"]["source_workload_refs"]}
        provenance = []
        for job_id in sorted(job_ids):
            job = self.store.job(conn, job_id)
            if not job or job["project"] != project:
                raise NotFound("source job not found")
            body = job["body"]
            binding = next(
                (
                    b
                    for b in space["bindings"]
                    if b["workload_ref"] in tasks
                    and b["workload_digest"] == signature(body["spec"])
                    and b["candidate_ref"] == body["candidate"]["ref"]
                ),
                None,
            )
            if not binding or body["request"]["mode"] == "pilot":
                raise Rejected("source must be an independent measured profile in this family")
            record = required(self.store, conn, "result", body["attempt_id"], project)
            profile = required(self.store, conn, "profile", body["attempt_id"], project)
            result = ExecutionResult.model_validate(record)
            if (
                job["state"] != "SUCCEEDED"
                or not body.get("quality_passed")
                or result.evidence_kind != "hardware"
                or result.outcome != "COMPLETED"
                or result.job_id != job_id
                or result.attempt_id != body["attempt_id"]
                or result.epoch != job["epoch"]
                or signature(result) != body.get("result_digest")
                or result.context_signature != binding["context_signature"]
                or body["context_signature"] != binding["context_signature"]
                or result.workload_signature != binding["workload_signature"]
                or signature(body["variant"]) != binding["variant_digest"]
                or signature(body["capability"]) != binding["capability_digest"]
                or body["candidate"] not in body["spec"]["candidates"]
                or profile["job_id"] != job_id
                or profile["candidate_ref"] != binding["candidate_ref"]
                or signature(profile["result"]) != signature(result)
            ):
                raise Rejected("source result/profile provenance mismatch")
            m, quality = result.measurements, body["spec"]["quality"]
            if (
                m is None
                or m.work_units != body["spec"]["identity"]["work_units"]
                or m.quality_value < quality["minimum"]
                or m.peak_memory_mib > quality["maximum_peak_memory_mib"]
            ):
                raise Rejected("source violates work, quality or memory contract")
            start, end = (
                datetime.fromisoformat(body["created_at"]),
                datetime.fromisoformat(body["finished_at"]),
            )
            max_age = min(
                space["request"]["maximum_source_age_seconds"], quality["max_profile_age_seconds"]
            )
            if (
                end > now()
                or end <= start
                or (end - start).total_seconds() < m.elapsed_seconds
                or (now() - end).total_seconds() > max_age
            ):
                raise Rejected("source evidence is stale or has invalid timing")
            spec, candidate, variant, cap = self.service.bundle(
                conn, project, binding["workload_ref"], binding["candidate_ref"]
            )
            if compatibility(spec, candidate, variant, cap, at=start):
                raise Rejected("source capability was invalid when execution was requested")
            thermal_digest = None
            if variant.thermal_policy:
                from .thermal import assess, validate_trace

                trace = required(self.store, conn, "thermal_trace", result.attempt_id, project)
                try:
                    checked = validate_trace(trace, result, body)
                    thermal = assess(checked, variant.thermal_policy.model_dump(mode="json"))
                except ValueError as exc:
                    raise Rejected("source thermal evidence is invalid") from exc
                if thermal["status"] != "ELIGIBLE_TRACE":
                    raise Rejected("source thermal evidence is ineligible")
                thermal_digest = signature(checked)
            tasks[binding["workload_ref"]].append(
                TransferObservation(
                    attempt_id=result.attempt_id,
                    candidate_ref=binding["candidate_ref"],
                    elapsed_seconds=m.elapsed_seconds,
                    evaluation_wall_seconds=(end - start).total_seconds(),
                    peak_memory_mib=m.peak_memory_mib,
                    quality_value=m.quality_value,
                    quality_passed=True,
                    memory_passed=True,
                )
            )
            provenance.append(
                {
                    "job_id": job_id,
                    "attempt_id": result.attempt_id,
                    "result_digest": signature(result),
                    "profile_digest": signature(profile),
                    "workload_ref": binding["workload_ref"],
                    "finished_at": body["finished_at"],
                    "thermal_trace_digest": thermal_digest,
                }
            )
        # Completeness, shared coordinates and disjoint-attempt checks without fitting.
        try:
            sources = [
                TransferTask(
                    workload_signature=next(
                        b["workload_signature"]
                        for b in space["bindings"]
                        if b["workload_ref"] == ref
                    ),
                    runtime_group_signature=space["runtime_group_signature"],
                    observations=rows,
                ).model_dump(mode="json")
                for ref, rows in tasks.items()
            ]
            self.problem(space, sources, [])
        except ValueError as exc:
            raise Rejected(
                "source requires independent repeated profiles at every configuration"
            ) from exc
        return sources, provenance

    def evidence(self, project, ref, request: TransferEvidenceRequest):
        request = TransferEvidenceRequest.model_validate(request.model_dump(mode="json"))
        with self.store.transaction() as conn:
            space = required(self.store, conn, "transfer_space", ref, project)
            previous = self.store.get(conn, "transfer_evidence", request.ref)
            if previous:
                if (
                    previous["project"] != project
                    or previous["body"]["space_digest"] != signature(space)
                    or previous["body"]["request"] != request.model_dump(mode="json")
                ):
                    raise Rejected("immutable transfer evidence already exists")
                return previous["body"]
            self._bindings(conn, project, space)
            sources, provenance = self._source_records(conn, project, space, request.job_ids)
            body = {
                "request": request.model_dump(mode="json"),
                "space_ref": ref,
                "space_digest": signature(space),
                "recorded_at": now().isoformat(),
                "sources": sources,
                "provenance": provenance,
                "historical_source_wall_seconds": sum(
                    r["evaluation_wall_seconds"] for t in sources for r in t["observations"]
                ),
                "historical_cost_recharged": False,
                "source_cost_scope": "selected source Jobs only; upstream search and training costs excluded",
                "execution_authorized": False,
            }
            return self.store.put(conn, "transfer_evidence", request.ref, project, body)["body"]

    def checked(self, conn, project, space_ref, evidence_ref):
        space = required(self.store, conn, "transfer_space", space_ref, project)
        evidence = required(self.store, conn, "transfer_evidence", evidence_ref, project)
        if evidence["space_digest"] != signature(space):
            raise Rejected("source evidence belongs to a different transfer space")
        self._bindings(conn, project, space)
        sources, provenance = self._source_records(
            conn, project, space, evidence["request"]["job_ids"]
        )
        if sources != evidence["sources"] or provenance != evidence["provenance"]:
            raise Rejected("frozen transfer evidence changed")
        return space, evidence

    @staticmethod
    def problem(space, sources, observations, seed=0):
        target_binding = next(
            b
            for b in space["bindings"]
            if b["workload_ref"] == space["request"]["target_workload_ref"]
        )
        return RGPEInput(
            runtime_group_signature=space["runtime_group_signature"],
            options=space["options"],
            feature_names=space["request"]["feature_names"],
            sources=sources,
            target=TransferTask(
                workload_signature=target_binding["workload_signature"],
                runtime_group_signature=space["runtime_group_signature"],
                observations=observations,
            ),
            minimum_quality=space["quality"]["minimum"],
            maximum_peak_memory_mib=space["quality"]["maximum_peak_memory_mib"],
            rank_tie_fraction=space["request"]["rank_tie_fraction"],
            maximum_source_rank_loss=space["request"]["maximum_source_rank_loss"],
            seed=seed,
        )
