"""Approved proportional sampling, execution-bound receipts and cooperative input IO.

Receipts attest what a qualified cooperative runner reports consuming. They are
not remote attestation or proof that operator-provided strata capture every
property of the source distribution.
"""

import hashlib
import math
import os
import stat
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract, Digest, Ref, WorkloadSpec, signature


class Sample(Contract):
    ref: Ref
    stratum: Ref
    content_digest: Digest


class SamplingPolicy(Contract):
    ref: Ref
    project_ref: Ref
    dataset_version: str = Field(min_length=1, max_length=256)
    input_shape: tuple[int, ...] = Field(min_length=1, max_length=8)
    precision: Literal["fp32", "fp16"]
    batch_size: int = Field(ge=1)
    seed: int = 0
    warmup_units: int = Field(default=3, ge=1, le=32)
    maximum_stratum_fraction_error: float = Field(default=0.05, gt=0, le=0.5)
    population: tuple[Sample, ...] = Field(min_length=4, max_length=128)
    method: Literal["proportional-largest-remainder-without-replacement-v1"] = (
        "proportional-largest-remainder-without-replacement-v1"
    )
    encoding: Literal["tensor-json-v1"] = "tensor-json-v1"

    @model_validator(mode="after")
    def distinct_population(self):
        if any(n <= 0 for n in self.input_shape) or math.prod(self.input_shape) > 131072:
            raise ValueError("positive bounded input shape required")
        if len({s.ref for s in self.population}) != len(self.population):
            raise ValueError("sample identifiers must be unique")
        if len({s.content_digest for s in self.population}) != len(self.population):
            raise ValueError("identical-input repetition is not representative sampling")
        if not 2 <= len({s.stratum for s in self.population}) <= 16:
            raise ValueError("initial policy requires two to sixteen declared strata")
        return self


def selection(policy: SamplingPolicy, units: int):
    policy = SamplingPolicy.model_validate(policy.model_dump(mode="json"))
    if (
        isinstance(units, bool)
        or not isinstance(units, int)
        or not 1 <= units <= len(policy.population)
    ):
        raise ValueError("sample budget outside original population")
    population = Counter(s.stratum for s in policy.population)
    total = len(policy.population)
    quotas = {s: units * count // total for s, count in population.items()}
    order = sorted(
        population, key=lambda s: (-(units * population[s] % total), signature([policy.seed, s]))
    )
    for stratum in order[: units - sum(quotas.values())]:
        quotas[stratum] += 1
    if any(count == 0 for count in quotas.values()):
        raise ValueError("budget cannot represent every declared stratum")
    if any(
        abs(quotas[s] / units - population[s] / total)
        > policy.maximum_stratum_fraction_error + 1e-12
        for s in population
    ):
        raise ValueError("budget violates approved stratum fraction tolerance")

    def rank(sample):
        return signature(["sample-order-v1", policy.seed, sample.ref, sample.content_digest])

    picked = []
    for stratum in sorted(population):
        members = sorted((s for s in policy.population if s.stratum == stratum), key=rank)
        picked.extend(members[: quotas[stratum]])
    return tuple(sorted(picked, key=rank))


class SamplingPlan(Contract):
    policy_ref: Ref
    policy_digest: Digest
    population_digest: Digest
    population_size: int = Field(ge=4, le=128)
    dataset_version: str
    input_shape: tuple[int, ...]
    precision: Literal["fp32", "fp16"]
    batch_size: int = Field(ge=1)
    seed: int
    warmup_units: int = Field(ge=1, le=32)
    work_units: int = Field(ge=1, le=128)
    samples: tuple[Sample, ...] = Field(min_length=1, max_length=128)
    selection_digest: Digest

    @model_validator(mode="after")
    def complete_plan(self):
        if self.work_units > self.population_size:
            raise ValueError("plan work budget exceeds original population")
        if (
            len(self.samples) != self.work_units
            or len({s.ref for s in self.samples}) != self.work_units
        ):
            raise ValueError("plan must enumerate every distinct measured batch")
        if self.selection_digest != signature([s.model_dump(mode="json") for s in self.samples]):
            raise ValueError("selection digest mismatch")
        if (
            not self.input_shape
            or any(n <= 0 for n in self.input_shape)
            or math.prod(self.input_shape) > 131072
        ):
            raise ValueError("invalid plan shape")
        return self


class SamplingBinding(Contract):
    workload_ref: Ref
    workload_digest: Digest
    project_ref: Ref
    plan: SamplingPlan


def bind(policy, spec):
    policy = SamplingPolicy.model_validate(policy)
    spec = WorkloadSpec.model_validate(spec)
    identity = spec.identity
    if (
        policy.project_ref != spec.project_ref
        or identity.sampling_policy_digest != signature(policy)
        or identity.task_type not in {"inference", "benchmark"}
        or identity.dataset_version != policy.dataset_version
        or identity.input_shape != policy.input_shape
        or identity.precision != policy.precision
        or identity.batch_size != policy.batch_size
        or identity.seed != policy.seed
    ):
        raise ValueError("sampling policy does not match the workload identity")
    samples = selection(policy, identity.work_units)
    plan = SamplingPlan(
        policy_ref=policy.ref,
        policy_digest=signature(policy),
        population_digest=signature(
            [s.model_dump(mode="json") for s in sorted(policy.population, key=lambda s: s.ref)]
        ),
        population_size=len(policy.population),
        dataset_version=policy.dataset_version,
        input_shape=policy.input_shape,
        precision=policy.precision,
        batch_size=policy.batch_size,
        seed=policy.seed,
        warmup_units=policy.warmup_units,
        work_units=identity.work_units,
        samples=samples,
        selection_digest=signature([s.model_dump(mode="json") for s in samples]),
    )
    return SamplingBinding(
        workload_ref=spec.ref,
        workload_digest=signature(spec),
        project_ref=spec.project_ref,
        plan=plan,
    )


class SamplingBindingRequest(Contract):
    workload_ref: Ref
    policy_ref: Ref


class SamplingPolicies:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def bind(self, project, request):
        from .service import Rejected, required

        with self.store.transaction() as conn:
            policy = required(self.store, conn, "sampling_policy", request.policy_ref, project)
            spec = required(self.store, conn, "workload", request.workload_ref, project)
            try:
                binding = bind(policy, spec)
            except ValueError as exc:
                raise Rejected("sampling binding rejected: " + str(exc)) from exc
            return self.store.put(
                conn,
                "sampling_binding",
                request.workload_ref,
                project,
                binding.model_dump(mode="json"),
            )["body"]

    def checked(self, conn, project, workload_ref):
        from .service import Rejected, required

        value = required(self.store, conn, "sampling_binding", workload_ref, project)
        policy = required(self.store, conn, "sampling_policy", value["plan"]["policy_ref"], project)
        spec = required(self.store, conn, "workload", workload_ref, project)
        try:
            expected = bind(policy, spec).model_dump(mode="json")
        except ValueError as exc:
            raise Rejected("sampling policy no longer matches the workload") from exc
        if value != expected:
            raise Rejected("sampling binding no longer matches the immutable policy/workload")
        return expected


class ConsumedSample(Contract):
    ref: Ref
    content_digest: Digest


class SamplingReceipt(Contract):
    job_id: Ref
    attempt_id: Ref
    result_digest: Digest
    policy_digest: Digest
    selection_digest: Digest
    warmup_units: int = Field(ge=1, le=32)
    measured_samples: tuple[ConsumedSample, ...] = Field(min_length=1, max_length=128)


def validate_receipt(receipt, result, binding):
    receipt = SamplingReceipt.model_validate(receipt)
    binding = SamplingBinding.model_validate(binding)
    plan = binding.plan
    expected = tuple(
        ConsumedSample(ref=s.ref, content_digest=s.content_digest) for s in plan.samples
    )
    if (
        result.outcome != "COMPLETED"
        or result.measurements is None
        or receipt.job_id != result.job_id
        or receipt.attempt_id != result.attempt_id
        or receipt.result_digest != signature(result)
        or receipt.policy_digest != plan.policy_digest
        or receipt.selection_digest != plan.selection_digest
        or receipt.warmup_units != plan.warmup_units
        or receipt.measured_samples != expected
        or result.measurements.work_units != plan.work_units
        or result.measurements.sample_count != plan.work_units
    ):
        raise ValueError("sampling receipt differs from the approved measured input sequence")
    return receipt


class TensorBatch(Contract):
    shape: tuple[int, ...] = Field(min_length=1, max_length=8)
    precision: Literal["fp32", "fp16"]
    values: tuple[Annotated[float, Field(strict=True)], ...] = Field(
        min_length=1, max_length=131072
    )

    @model_validator(mode="after")
    def complete_tensor(self):
        if any(n <= 0 for n in self.shape) or math.prod(self.shape) != len(self.values):
            raise ValueError("tensor bytes do not describe the declared shape")
        limit = 65504 if self.precision == "fp16" else 3.4028234663852886e38
        if any(abs(v) > limit for v in self.values):
            raise ValueError("tensor values exceed the declared finite precision range")
        return self


def directory_reader(root: Path):
    def read(sample):
        # A ConfigMap symlink is deliberately not accepted: qualify a regular-file
        # read-only dataset image/PVC or a bounded copy before using this reader.
        fd = os.open(root / (sample.ref + ".json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise ValueError("regular sample file required")
            data = f.read(1048577)
        if len(data) > 1048576:
            raise ValueError("sample exceeds the bounded tensor JSON contract")
        return data

    return read


class SamplingSession:
    """Hash and decode the exact bytes passed to a cooperative measured operation."""

    def __init__(self, plan):
        self.plan = SamplingPlan.model_validate(plan)
        self.measured = []
        self.warmup_completed = 0
        self.failed = False

    def _consume(self, sample, reader, operation):
        if self.failed:
            raise ValueError("failed sampling session cannot be resumed")
        try:
            data = reader(sample)
            if not isinstance(data, bytes) or len(data) > 1048576:
                raise ValueError("bounded input bytes required")
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if digest != sample.content_digest:
                raise ValueError("actual input content digest differs from the manifest")
            batch = TensorBatch.model_validate_json(data)
            if batch.shape != self.plan.input_shape or batch.precision != self.plan.precision:
                raise ValueError("decoded input shape or precision differs from the policy")
            value = operation(batch)
        except BaseException:
            self.failed = True
            raise
        return value, ConsumedSample(ref=sample.ref, content_digest=digest)

    def warmup(self, reader, operation):
        if self.warmup_completed:
            raise ValueError("warmup may only be performed once")
        for _ in range(self.plan.warmup_units):
            self._consume(self.plan.samples[0], reader, operation)
            self.warmup_completed += 1

    def measure_next(self, reader, operation):
        if (
            self.warmup_completed != self.plan.warmup_units
            or len(self.measured) >= self.plan.work_units
        ):
            raise ValueError("warmup incomplete or measured budget exhausted")
        value, consumed = self._consume(self.plan.samples[len(self.measured)], reader, operation)
        self.measured.append(consumed)
        return value

    def receipt(self, result):
        if self.failed or len(self.measured) != self.plan.work_units:
            raise ValueError("only an entirely consumed successful session can emit a receipt")
        receipt = SamplingReceipt(
            job_id=result.job_id,
            attempt_id=result.attempt_id,
            result_digest=signature(result),
            policy_digest=self.plan.policy_digest,
            selection_digest=self.plan.selection_digest,
            warmup_units=self.warmup_completed,
            measured_samples=tuple(self.measured),
        )
        if (
            result.outcome != "COMPLETED"
            or result.measurements is None
            or result.measurements.work_units != self.plan.work_units
            or result.measurements.sample_count != self.plan.work_units
        ):
            raise ValueError("result work count differs from consumed batches")
        return receipt
