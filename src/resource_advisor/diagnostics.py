"""Evidence-bound hypotheses from non-overlapping cooperative phase timings.

These are wall-time hypotheses, not causal diagnoses or hardware utilization.
No resource change is applied. Existing ExecutionResult digests stay unchanged.
"""

import math
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, Digest, Ref, signature

Phase = Literal[
    "input_wait",
    "cpu_processing",
    "file_read",
    "host_to_device",
    "accelerator_compute",
    "device_to_host",
    "synchronization",
]


class PhaseSample(Contract):
    wall_seconds: float = Field(gt=0)
    phases_seconds: dict[Phase, float] = Field(min_length=1, max_length=7)

    @model_validator(mode="after")
    def serial_intervals(self):
        values = self.phases_seconds.values()
        if any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("phase durations must be finite and nonnegative")
        if sum(values) > self.wall_seconds + max(1e-9, self.wall_seconds * 1e-6):
            raise ValueError("overlapping or out-of-bound phase intervals")
        return self


class PhaseProfile(Contract):
    schema_version: Literal["v1"] = "v1"
    job_id: Ref
    attempt_id: Ref
    result_digest: Digest
    measurement_boundary: str = Field(min_length=1, max_length=256)
    instrumentation: Literal["cooperative_serial"] = "cooperative_serial"
    samples: tuple[PhaseSample, ...] = Field(min_length=1, max_length=100)


def validate_profile(profile, result, boundary):
    profile = PhaseProfile.model_validate(profile)
    if (
        profile.job_id != result.job_id
        or profile.attempt_id != result.attempt_id
        or profile.result_digest != signature(result)
        or profile.measurement_boundary != boundary
        or result.measurements is None
    ):
        raise ValueError("phase profile does not match completed result and boundary")
    m = result.measurements
    if len(profile.samples) != m.sample_count or len(profile.samples) != m.work_units:
        raise ValueError("phase profile must cover every measured work unit")
    if not math.isclose(
        sum(s.wall_seconds for s in profile.samples), m.elapsed_seconds, rel_tol=1e-6, abs_tol=1e-9
    ):
        raise ValueError("phase wall time differs from result boundary")
    return profile


def diagnose(profile=None, *, evidence_kind=None):
    report = {
        "policy_version": "serial-phases-v1",
        "status": "INSUFFICIENT_EVIDENCE",
        "hypothesis": None,
        "auto_apply": False,
        "resource_change": None,
        "reasons": [],
        "phase_shares": {},
        "coverage": None,
        "limitations": [
            "Phase wall time includes software/launch/wait overhead; it is not kernel busy time.",
            "One run suggests a hypothesis; independent one-variable trials are required.",
            "Missing phases are unknown, not zero; overlapping pipelines need separate analysis.",
        ],
    }
    if profile is None:
        report["reasons"] = ["NO_PHASE_PROFILE"]
        return report
    profile = PhaseProfile.model_validate(profile)
    report.update(
        attempt_id=profile.attempt_id,
        result_digest=profile.result_digest,
        profile_digest=signature(profile),
        sample_count=len(profile.samples),
    )
    if evidence_kind != "hardware":
        report["reasons"] = ["HARDWARE_EVIDENCE_REQUIRED"]
        return report
    if len(profile.samples) < 3:
        report["reasons"] = ["TOO_FEW_PHASE_SAMPLES"]
        return report
    phases = set.intersection(*(set(s.phases_seconds) for s in profile.samples))
    total = sum(s.wall_seconds for s in profile.samples)
    shares = {p: sum(s.phases_seconds[p] for s in profile.samples) / total for p in phases}
    coverage = min(1.0, sum(shares.values()))
    report.update(phase_shares=shares, coverage=coverage, unclassified_share=1 - coverage)
    if coverage < 0.8:
        report["reasons"] = ["INCOMPLETE_PHASE_COVERAGE"]
        return report
    groups = {
        "POSSIBLE_INPUT_SUPPLY_BOUND": ("input_wait", "cpu_processing"),
        "POSSIBLE_IO_BOUND": ("file_read",),
        "POSSIBLE_TRANSFER_BOUND": ("host_to_device", "device_to_host"),
        "POSSIBLE_ACCELERATOR_PATH_BOUND": ("accelerator_compute",),
        "POSSIBLE_SYNCHRONIZATION_BOUND": ("synchronization",),
    }
    dominant = max(groups, key=lambda k: sum(shares.get(p, 0) for p in groups[k]))
    selected = groups[dominant]
    share = sum(shares.get(p, 0) for p in selected)
    # Require the same dominance in most steps, not just one exceptional stall.
    agreeing = sum(
        sum(s.phases_seconds.get(p, 0) for p in selected) / s.wall_seconds >= 0.6
        for s in profile.samples
    )
    if share < 0.6 or agreeing / len(profile.samples) < 0.8:
        report["reasons"] = ["MIXED_OR_UNSTABLE_PHASE_DOMINANCE"]
        return report
    report.update(
        status="HYPOTHESIS",
        hypothesis=dominant,
        reasons=["SERIAL_PHASE_SHARE_GE_60_PERCENT", "DOMINANT_IN_GE_80_PERCENT_STEPS"],
        dominant_share=share,
        next_action="Request an approved one-variable trial; preserve GPU count.",
    )
    return report
