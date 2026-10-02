"""Immutable workload bindings for MF analysis; registration is not qualification."""

from datetime import datetime
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from .contracts import Contract, Digest, ExecutionResult, Ref, now, signature
from .mfkg import MFKernelInput, MFObservation, MFOption
from .policy import compatibility, context_signature
from .service import NotFound, Rejected, required


class FidelitySpace(Contract):
    ref: Ref
    project_ref: Ref
    target_workload_ref: Ref
    lower_workload_refs: tuple[Ref, ...] = Field(min_length=1, max_length=3)
    feature_names: tuple[Literal["host_cpu", "host_memory_mib"], ...] = Field(
        min_length=1, max_length=2
    )
    fidelity_axis: Literal["identical_input_repetition", "representative_sampling"]
    sampling_policy_digest: Digest | None = None

    @model_validator(mode="after")
    def unique_levels(self):
        levels = (self.target_workload_ref, *self.lower_workload_refs)
        if len(set(levels)) != len(levels) or len(set(self.feature_names)) != len(
            self.feature_names
        ):
            raise ValueError("workloads and feature names must be unique")
        if (self.fidelity_axis == "representative_sampling") != (
            self.sampling_policy_digest is not None
        ):
            raise ValueError("representative sampling requires its declared policy digest")
        return self


class FidelityEvidenceRequest(Contract):
    job_ids: tuple[Ref, ...] = Field(min_length=8, max_length=192)
    seed: int = 0

    @model_validator(mode="after")
    def distinct_jobs(self):
        if len(set(self.job_ids)) != len(self.job_ids):
            raise ValueError("independent Jobs must be unique")
        return self


class FidelitySpaces:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def create(self, project, request: FidelitySpace):
        request = FidelitySpace.model_validate(request.model_dump(mode="json"))
        if request.project_ref != project:
            raise Rejected("space project differs from principal")
        with self.store.transaction() as conn:
            previous = self.store.get(conn, "fidelity_space", request.ref)
            if previous:
                if previous["project"] != project or previous["body"][
                    "request"
                ] != request.model_dump(mode="json"):
                    raise Rejected("immutable fidelity space already exists")
                return previous["body"]
            target = required(self.store, conn, "workload", request.target_workload_ref, project)
            target_refs = {c["ref"] for c in target["candidates"]}
            if not 2 <= len(target_refs) <= 8:
                raise Rejected("fidelity space requires two to eight configurations")
            target_units = target["identity"]["work_units"]
            coordinates, bounds = {}, {}
            for name in request.feature_names:
                values = [c["context"]["resources"][name] for c in target["candidates"]]
                lo, hi = min(values), max(values)
                if lo == hi:
                    raise Rejected("each numeric feature must vary in the target space")
                bounds[name] = [lo, hi]
            for c in target["candidates"]:
                coordinates[c["ref"]] = tuple(
                    (c["context"]["resources"][name] - bounds[name][0])
                    / (bounds[name][1] - bounds[name][0])
                    for name in request.feature_names
                )
            if len(set(coordinates.values())) != len(target_refs):
                raise Rejected("numeric features must distinguish every configuration")
            options, bindings, common = [], [], None
            levels = [request.target_workload_ref, *request.lower_workload_refs]
            unit_levels = set()
            for workload_ref in levels:
                raw = required(self.store, conn, "workload", workload_ref, project)
                if {c["ref"] for c in raw["candidates"]} != target_refs:
                    raise Rejected("every fidelity must expose the same configuration refs")
                if raw["baseline_candidate_ref"] != target["baseline_candidate_ref"]:
                    raise Rejected("fidelity levels must preserve the target baseline")
                units = raw["identity"]["work_units"]
                if units in unit_levels or (
                    workload_ref != request.target_workload_ref and units >= target_units
                ):
                    raise Rejected("lower fidelities need distinct smaller work budgets")
                unit_levels.add(units)
                for candidate_ref in sorted(target_refs):
                    spec, candidate, variant, cap = self.service.bundle(
                        conn, project, workload_ref, candidate_ref
                    )
                    errors = compatibility(spec, candidate, variant, cap)
                    if errors:
                        raise Rejected("fidelity compatibility: " + ",".join(errors))
                    if spec.identity.task_type not in {"inference", "benchmark"}:
                        raise Rejected(
                            "training fidelity requires checkpoint/convergence qualification"
                        )
                    if not spec.profiling.consent or variant.device_class not in {"gpu", "npu"}:
                        raise Rejected("explicit accelerator profiling consent required")
                    if not variant.pilot_command or variant.pilot_command != variant.command:
                        raise Rejected(
                            "fidelity workload must use the same bounded pilot and normal entrypoint"
                        )
                    target_candidate = next(
                        c for c in target["candidates"] if c["ref"] == candidate_ref
                    )
                    if candidate.context.model_dump(mode="json") != target_candidate["context"]:
                        raise Rejected("a configuration must stay identical across fidelity levels")
                    context = candidate.context.model_dump(mode="json")
                    for feature in request.feature_names:
                        context["resources"].pop(feature)
                    invariant = {
                        "identity": spec.identity.model_dump(mode="json", exclude={"work_units"}),
                        "quality": spec.quality.model_dump(mode="json"),
                        "context": context,
                        "backend": candidate.backend,
                        "cluster": cap.backend_cluster_id,
                        "node": cap.node_ref,
                        "image": variant.image,
                        "command": variant.command,
                        "compiled_artifact": variant.compiled_artifact_digest,
                        "device_class": variant.device_class,
                        "vendor": variant.accelerator_vendor,
                    }
                    current = signature(invariant)
                    if common is not None and common != current:
                        raise Rejected(
                            "mixed runtime, input, quality or unmodeled configuration change"
                        )
                    common = current
                    option_ref = (
                        "option-" + signature([request.ref, workload_ref, candidate_ref])[7:39]
                    )
                    options.append(
                        MFOption(
                            ref=option_ref,
                            candidate_ref=candidate_ref,
                            coordinates=coordinates[candidate_ref],
                            fidelity=units / target_units,
                        ).model_dump(mode="json")
                    )
                    bindings.append(
                        {
                            "option_ref": option_ref,
                            "workload_ref": workload_ref,
                            "candidate_ref": candidate_ref,
                            "work_units": units,
                            "workload_digest": signature(spec),
                            "variant_digest": signature(variant),
                            "capability_digest": signature(cap),
                            "context_signature": context_signature(candidate, variant),
                        }
                    )
            body = {
                "request": request.model_dump(mode="json"),
                "registered_at": now().isoformat(),
                "runtime_group_signature": common,
                "feature_bounds": bounds,
                "options": options,
                "bindings": bindings,
                "execution_authorized": False,
                "qualification_reasons": [
                    "REPRESENTATIVE_SAMPLING_RECEIPTS_AND_PAIRED_THERMAL_QUALIFICATION_REQUIRED"
                    if request.fidelity_axis == "representative_sampling"
                    else "REPLICATION_ONLY_NOT_MULTI_FIDELITY"
                ],
            }
            return self.store.put(conn, "fidelity_space", request.ref, project, body)["body"]

    def get(self, project, ref):
        with self.store.transaction() as conn:
            return required(self.store, conn, "fidelity_space", ref, project)

    def _check_bindings(self, conn, project, space, *, current):
        for binding in space["bindings"]:
            spec, candidate, variant, cap = self.service.bundle(
                conn, project, binding["workload_ref"], binding["candidate_ref"]
            )
            if (
                signature(spec) != binding["workload_digest"]
                or signature(variant) != binding["variant_digest"]
                or signature(cap) != binding["capability_digest"]
            ):
                raise Rejected("immutable fidelity binding changed")
            if current and compatibility(spec, candidate, variant, cap):
                raise Rejected("fidelity execution context is no longer compatible")

    def resolve(self, project, ref, option_ref):
        """Read-only preview; a numerical choice never becomes a Job authorization."""
        with self.store.transaction() as conn:
            space = required(self.store, conn, "fidelity_space", ref, project)
            self._check_bindings(conn, project, space, current=True)
            binding = next((b for b in space["bindings"] if b["option_ref"] == option_ref), None)
            if binding is None:
                raise NotFound("option not found")
            target = next(
                b
                for b in space["bindings"]
                if b["candidate_ref"] == binding["candidate_ref"]
                and b["workload_ref"] == space["request"]["target_workload_ref"]
            )
            return {
                "space_digest": signature(space),
                "measurement_binding": binding,
                "independent_confirmation_binding": target,
                "execution_authorized": False,
                "qualification_reasons": space["qualification_reasons"],
            }

    def evidence(self, project, ref, request: FidelityEvidenceRequest):
        """Join immutable Jobs/results, never accept caller-supplied performance numbers."""
        request = FidelityEvidenceRequest.model_validate(request.model_dump(mode="json"))
        with self.store.transaction() as conn:
            space = required(self.store, conn, "fidelity_space", ref, project)
            self._check_bindings(conn, project, space, current=False)
            observations, provenance = [], []
            for job_id in sorted(request.job_ids):
                job = self.store.job(conn, job_id)
                if not job or job["project"] != project:
                    raise NotFound("job not found")
                body = job["body"]
                binding = next(
                    (
                        b
                        for b in space["bindings"]
                        if b["workload_digest"] == signature(body["spec"])
                        and b["candidate_ref"] == body["candidate"]["ref"]
                    ),
                    None,
                )
                if not binding or body["request"]["mode"] == "confirmation":
                    raise Rejected("job outside space or reserved independent confirmation")
                record = required(self.store, conn, "result", body["attempt_id"], project)
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
                    or result.workload_signature != signature(body["spec"]["identity"])
                    or signature(body["variant"]) != binding["variant_digest"]
                    or signature(body["capability"]) != binding["capability_digest"]
                    or body["candidate"] not in body["spec"]["candidates"]
                ):
                    raise Rejected("unqualified or inconsistent hardware result")
                m, quality = result.measurements, body["spec"]["quality"]
                if (
                    m.work_units != binding["work_units"]
                    or m.quality_value < quality["minimum"]
                    or m.peak_memory_mib > quality["maximum_peak_memory_mib"]
                ):
                    raise Rejected("result violates work, quality or memory contract")
                start, end = (
                    datetime.fromisoformat(body["created_at"]),
                    datetime.fromisoformat(body["finished_at"]),
                )
                if (
                    end > now()
                    or end <= start
                    or (end - start).total_seconds() < m.elapsed_seconds
                    or (now() - end).total_seconds() > quality["max_profile_age_seconds"]
                ):
                    raise Rejected("stale or invalid evaluation interval")
                observations.append(
                    MFObservation(
                        attempt_id=result.attempt_id,
                        option_ref=binding["option_ref"],
                        runtime_group_signature=space["runtime_group_signature"],
                        seconds_per_work_unit=m.elapsed_seconds / m.work_units,
                        evaluation_wall_seconds=(end - start).total_seconds(),
                        quality_passed=True,
                        memory_passed=True,
                    )
                )
                provenance.append(
                    {
                        "job_id": job_id,
                        "attempt_id": result.attempt_id,
                        "result_digest": signature(result),
                        "workload_digest": binding["workload_digest"],
                        "created_at": body["created_at"],
                        "finished_at": body["finished_at"],
                    }
                )
            # Use the kernel's completeness/independence checks, without fitting or authorizing it.
            try:
                numerical = MFKernelInput(
                    runtime_group_signature=space["runtime_group_signature"],
                    fidelity_axis="representative_sampling",
                    options=tuple(space["options"]),
                    observations=tuple(observations),
                    feature_names=space["request"]["feature_names"],
                    seed=request.seed,
                )
            except ValidationError as exc:
                raise Rejected("incomplete or non-independent fidelity evidence") from exc
            repetition = space["request"]["fidelity_axis"] == "identical_input_repetition"
            report = {
                "space_ref": ref,
                "space_digest": signature(space),
                "provenance": provenance,
                "observations": [o.model_dump(mode="json") for o in observations],
                "kernel_input": None if repetition else numerical.model_dump(mode="json"),
                "execution_authorized": False,
                "qualification_reasons": space["qualification_reasons"],
                "cost_semantics": "Job creation through validated result ingestion; external compilation/data staging costs excluded",
                "historical_cost_reused": True,
                "sampling_policy_verified": False,
            }
            report["ref"] = "mf-evidence-" + signature(report)[7:39]
            return self.store.put(conn, "mf_evidence", report["ref"], project, report)["body"]
