"""Versioned, fail-closed contracts shared by API, collectors and advisors."""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Ref = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


def now() -> datetime:
    return datetime.now(UTC)


def signature(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Backend(StrEnum):
    KUBERNETES = "kubernetes"
    SLURM = "slurm"


class State(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    SUBMITTING = "SUBMITTING"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COLLECTING = "COLLECTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RESULT_INVALID = "RESULT_INVALID"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"


TERMINAL = {State.SUCCEEDED, State.FAILED, State.RESULT_INVALID, State.CANCELED}


class WorkloadIdentity(Contract):
    code_digest: Digest
    model_digest: Digest
    dataset_version: str = Field(min_length=1, max_length=256)
    config_digest: Digest
    task_type: Literal["training", "inference", "benchmark", "preprocessing"]
    input_shape: tuple[int, ...] = Field(min_length=1)
    batch_size: int = Field(ge=1)
    global_batch_size: int | None = Field(default=None, ge=1)
    gradient_accumulation: int = Field(default=1, ge=1)
    precision: str = Field(min_length=1)
    seed: int = 0
    work_units: int = Field(ge=1)
    quality_contract_digest: Digest
    measurement_boundary: str = Field(min_length=1)

    @model_validator(mode="after")
    def positive_shape(self):
        if any(n <= 0 for n in self.input_shape):
            raise ValueError("input shape dimensions must be positive")
        return self


class Resources(Contract):
    host_cpu: float = Field(gt=0, le=1024)
    host_memory_mib: int = Field(ge=32, le=1048576)
    accelerator_count: int = Field(ge=0, le=64)


class ExecutionContext(Contract):
    arch: Literal["amd64", "arm64"]
    environment_digest: Digest
    runtime_versions: dict[str, str]
    accelerator_model: str
    memory_model: Literal["discrete", "unified", "host"]
    power_mode: str
    allocation_mode: Literal["physical_device", "virtual_slot", "cpu_only"]
    resources: Resources
    parameters: dict[str, int | float | str] = Field(default_factory=dict)


class RuntimeVariant(Contract):
    ref: Ref
    workload_ref: Ref
    project_ref: Ref
    workload_signature: Digest
    arch: Literal["amd64", "arm64"]
    accelerator_vendor: str
    device_class: Literal["gpu", "npu", "cpu"]
    precision: str
    model_digest: Digest
    compiled_artifact_digest: Digest | None = None
    image: str | None = None
    environment_digest: Digest
    command: tuple[str, ...] = Field(min_length=1)
    pilot_command: tuple[str, ...] | None = None
    verification: Literal["DETECTED", "RUNTIME_VERIFIED", "MODEL_VERIFIED", "TRAINING_VERIFIED"]
    validation_refs: tuple[Ref, ...] = ()
    supported_shapes: tuple[tuple[int, ...], ...]
    runtime_versions: dict[str, str]

    @model_validator(mode="after")
    def immutable_environment(self):
        if self.image is not None:
            if "@sha256:" not in self.image:
                raise ValueError("container images must be pinned by digest")
            digest = self.image.rsplit("@", 1)[1]
            if len(digest) != 71 or any(c not in "0123456789abcdef" for c in digest[7:]):
                raise ValueError("invalid image digest")
        if self.verification != "DETECTED" and not self.validation_refs:
            raise ValueError("verification requires evidence references")
        if any("\x00" in a for a in self.command + (self.pilot_command or ())):
            raise ValueError("NUL in command")
        return self


class CapabilitySnapshot(Contract):
    ref: Ref
    node_ref: Ref
    backend_cluster_id: Ref
    backend: Backend
    observed_at: AwareDatetime
    valid_for_seconds: int = Field(default=180, ge=1, le=86400)
    ready: bool
    arch: Literal["amd64", "arm64"]
    accelerator_vendor: str
    accelerator_model: str
    device_class: Literal["gpu", "npu", "cpu"]
    allocation_mode: Literal["physical_device", "virtual_slot", "cpu_only"]
    allocatable_count: int = Field(ge=0)
    host_cpu: float = Field(gt=0)
    host_memory_mib: int = Field(gt=0)
    resource_key: str | None = None
    runtime_versions: dict[str, str]
    environment_digest: Digest
    memory_model: Literal["discrete", "unified", "host"]
    power_mode: str
    reservation_verified: bool = False
    isolation_verified: bool = False
    evidence_refs: tuple[Ref, ...] = ()


class Candidate(Contract):
    ref: Ref
    variant_ref: Ref
    capability_ref: Ref
    backend: Backend
    context: ExecutionContext


class ProfilingPolicy(Contract):
    consent: bool = False
    max_candidates: int = Field(default=2, ge=1, le=64)
    max_wall_seconds_per_candidate: int = Field(default=180, ge=1, le=3600)
    max_queue_seconds: int = Field(default=300, ge=1, le=86400)
    total_wall_seconds: int = Field(default=600, ge=1, le=86400)
    final_validation_seconds: int = Field(default=180, ge=1, le=3600)
    mutable_parameters: tuple[str, ...] = ()
    checkpoint_digest: Digest | None = None
    max_probes: int = Field(default=8, ge=1, le=128)
    device_seconds: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def positive_device_budgets(self):
        if any(not key or value <= 0 for key, value in self.device_seconds.items()):
            raise ValueError("device budgets require nonempty units and positive seconds")
        return self


class QualityPolicy(Contract):
    metric: str = "accuracy"
    minimum: float
    minimum_repeats: int = Field(default=3, ge=1, le=100)
    maximum_peak_memory_mib: float = Field(gt=0)
    max_profile_age_seconds: int = Field(default=86400, ge=1)


class ExecutionPolicy(Contract):
    max_run_seconds: int = Field(default=3600, ge=1, le=86400)
    max_queue_seconds: int = Field(default=600, ge=1, le=86400)
    max_collection_seconds: int = Field(default=300, ge=1, le=3600)


class WorkloadSpec(Contract):
    schema_version: Literal["v1"] = "v1"
    ref: Ref
    project_ref: Ref
    identity: WorkloadIdentity
    candidates: tuple[Candidate, ...] = Field(min_length=1)
    baseline_candidate_ref: Ref
    profiling: ProfilingPolicy = Field(default_factory=ProfilingPolicy)
    quality: QualityPolicy
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)

    @model_validator(mode="after")
    def approved_space(self):
        refs = [c.ref for c in self.candidates]
        if len(refs) != len(set(refs)) or self.baseline_candidate_ref not in refs:
            raise ValueError("candidate refs must be unique and include the baseline")
        if self.identity.quality_contract_digest != signature(self.quality):
            raise ValueError("quality contract digest mismatch")
        baseline = next(c for c in self.candidates if c.ref == self.baseline_candidate_ref)
        for candidate in self.candidates:
            a, b = baseline.context, candidate.context
            if a.resources.accelerator_count != b.resources.accelerator_count:
                raise ValueError("accelerator count search is outside the initial contract")
            for key in ["host_cpu", "host_memory_mib"]:
                if getattr(a.resources, key) != getattr(b.resources, key):
                    if key not in self.profiling.mutable_parameters:
                        raise ValueError(f"unapproved resource change: {key}")
            for key in a.parameters.keys() | b.parameters.keys():
                if a.parameters.get(key) != b.parameters.get(key):
                    if key not in self.profiling.mutable_parameters:
                        raise ValueError(f"unapproved parameter change: {key}")
        return self


class Measurements(Contract):
    elapsed_seconds: float = Field(gt=0)
    peak_memory_mib: float = Field(ge=0)
    quality_value: float
    sample_count: int = Field(ge=1)
    work_units: int = Field(ge=1)
    latency_p50_ms: float | None = Field(default=None, ge=0)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    latency_p99_ms: float | None = Field(default=None, ge=0)
    throughput: float | None = Field(default=None, ge=0)
    gpu_utilization: float | None = Field(default=None, ge=0, le=100)
    power_watts: float | None = Field(default=None, ge=0)
    temperature_celsius: float | None = None


class ExecutionResult(Contract):
    schema_version: Literal["v1"] = "v1"
    job_id: Ref
    attempt_id: Ref
    epoch: int = Field(ge=1)
    workload_signature: Digest
    context_signature: Digest
    outcome: Literal["COMPLETED", "OOM", "TIMEOUT", "FAILED", "PARTIAL"]
    measured: Literal[True] = True
    evidence_kind: Literal["hardware", "synthetic"]
    measurements: Measurements | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def outcome_is_not_a_prediction(self):
        if (self.outcome == "COMPLETED") != (self.measurements is not None):
            raise ValueError("only complete measured results have usable performance measurements")
        return self


class JobRequest(Contract):
    workload_ref: Ref
    candidate_ref: Ref
    mode: Literal["fixed", "observe", "pilot", "confirmation"] = "observe"
    approval_ref: Ref | None = None
    study_ref: Ref | None = None
    parent_run_ref: Ref | None = None
    probe_plan_ref: Ref | None = None


class RecommendationRequest(Contract):
    workload_ref: Ref


class ApprovalRequest(Contract):
    recommendation_digest: Digest
    candidate_ref: Ref


class StudyRequest(Contract):
    workload_ref: Ref
    strategy: Literal["lookup", "random", "qlognei", "mfkg", "rgpe"]
    seed: int = 0
