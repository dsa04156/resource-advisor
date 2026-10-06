"""Read-only observations of existing programs, separate from execution results.

Trusted operators bind external execution identities. Import never schedules,
changes an attempt, adds model profiles, certifies quality or charges allocation.
"""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select

from .contracts import Backend, Contract, Digest, Ref, now
from .service import NotFound, Rejected
from .store import entities

Count = Annotated[int, Field(strict=True, ge=0)]
Metric = Annotated[float, Field(ge=0)]


class PassiveTarget(Contract):
    backend: Backend
    backend_cluster_id: Ref
    node_ref: Ref
    external_job_ref: Ref
    external_job_uid: Ref | None = None
    native_submitted_at: AwareDatetime | None = None
    attempt_ref: Ref
    platform_job_id: Ref | None = None
    process_identity_digest: Digest

    @model_validator(mode="after")
    def backend_identity(self):
        if self.backend == Backend.KUBERNETES and self.external_job_uid is None:
            raise ValueError("Kubernetes observation requires the immutable Job UID")
        if self.backend == Backend.SLURM and (
            self.native_submitted_at is None or self.external_job_uid is not None
        ):
            raise ValueError("Slurm identity is cluster/job/submission time, not a Kubernetes UID")
        return self


class ProcessSample(Contract):
    cpu_seconds: Metric | None = None
    rss_bytes: Count | None = None
    read_bytes: Count | None = None
    write_bytes: Count | None = None
    errors: dict[
        Literal["cpu_seconds", "rss_bytes", "read_bytes", "write_bytes"],
        Literal["UNAVAILABLE", "PERMISSION_DENIED"],
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def availability(self):
        for name in ("cpu_seconds", "rss_bytes", "read_bytes", "write_bytes"):
            if (getattr(self, name) is None) != (name in self.errors):
                raise ValueError("process metric needs a value or explicit missing reason")
        return self


class NodeSample(Contract):
    scope: Literal["observer-proc-view"] = "observer-proc-view"
    cpu_total_ticks: Count | None = None
    cpu_idle_ticks: Count | None = None
    memory_total_bytes: Count | None = None
    memory_available_bytes: Count | None = None

    @model_validator(mode="after")
    def paired_counters(self):
        for total, available in (
            (self.cpu_total_ticks, self.cpu_idle_ticks),
            (self.memory_total_bytes, self.memory_available_bytes),
        ):
            if (total is None) != (available is None) or (total is not None and available > total):
                raise ValueError("node metric pair missing or inconsistent")
        return self


class DeviceSample(Contract):
    scope: Literal["physical-device-inclusive"] = "physical-device-inclusive"
    device_uuid_digest: Digest
    utilization_percent: float | None = Field(default=None, ge=0, le=100)
    memory_used_bytes: Count | None = None
    power_w: Metric | None = None
    temperature_c: float | None = Field(default=None, ge=0, le=150)
    errors: dict[
        Literal["utilization_percent", "memory_used_bytes", "power_w", "temperature_c"], int
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def availability(self):
        for name in ("utilization_percent", "memory_used_bytes", "power_w", "temperature_c"):
            if (getattr(self, name) is None) != (name in self.errors):
                raise ValueError("device metric needs a value or explicit missing reason")
        if any(code == 0 for code in self.errors.values()):
            raise ValueError("successful sensor query is not a missing reason")
        return self


class PassiveSample(Contract):
    started_seconds: Metric
    finished_seconds: Metric
    process: ProcessSample
    node: NodeSample
    device: DeviceSample | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.finished_seconds < self.started_seconds:
            raise ValueError("sample query timestamps reversed")
        return self


class PassiveImport(Contract):
    ref: Ref
    project_ref: Ref
    schema_version: Literal["passive-observation-v1"] = "passive-observation-v1"
    provider: Literal["linux-proc-read-only-v1"] = "linux-proc-read-only-v1"
    evidence_kind: Literal["hardware", "synthetic"]
    collector_digest: Digest
    target: PassiveTarget
    started_at: AwareDatetime
    finished_at: AwareDatetime
    requested_window_seconds: float = Field(gt=0, le=3600)
    interval_seconds: float = Field(ge=0.1, le=60)
    stop_reason: Literal["WINDOW_COMPLETE", "PROCESS_EXITED", "TARGET_CHANGED", "UNREADABLE"]
    device_unavailable_reason: Literal["NOT_REQUESTED", "NVML_UNAVAILABLE"] | None = "NOT_REQUESTED"
    samples: tuple[PassiveSample, ...] = Field(min_length=1, max_length=600)
    # Explicitly absent without model hooks; clients cannot promote telemetry.
    quality_value: None = None
    step_seconds: None = None
    throughput: None = None
    network_bytes: None = None
    recommendation_authorized: Literal[False] = False

    @model_validator(mode="after")
    def complete_window(self):
        if self.finished_at < self.started_at:
            raise ValueError("observation wall timestamps reversed")
        for a, b in zip(self.samples, self.samples[1:], strict=False):
            if b.started_seconds <= a.finished_seconds:
                raise ValueError("sample windows overlap or reverse")
        for key in ("cpu_seconds", "read_bytes", "write_bytes"):
            present = [
                getattr(s.process, key) for s in self.samples if getattr(s.process, key) is not None
            ]
            for left, right in zip(present, present[1:], strict=False):
                if right < left:
                    raise ValueError("process counter reset; start a separate observation")
        identities = {s.device.device_uuid_digest for s in self.samples if s.device}
        if len(identities) > 1:
            raise ValueError("physical device identity changed")
        if bool(identities) == (self.device_unavailable_reason is not None):
            raise ValueError(
                "physical device requires observations or an explicit unavailable reason"
            )
        if self.samples[-1].finished_seconds > 3601:
            raise ValueError("observation exceeds bounded collection limit")
        return self


def assessment(report):
    samples = report.samples
    first, last = samples[0], samples[-1]
    counters = {}
    for name in ("cpu_seconds", "read_bytes", "write_bytes"):
        values = [getattr(s.process, name) for s in samples]
        counters[name] = values[-1] - values[0] if len(values) > 1 and None not in values else None
    rss = [s.process.rss_bytes for s in samples if s.process.rss_bytes is not None]
    return {
        "sample_count": len(samples),
        "window_seconds": last.finished_seconds - first.started_seconds,
        "collector_query_seconds": sum(s.finished_seconds - s.started_seconds for s in samples),
        "process_cpu_seconds": counters["cpu_seconds"],
        "process_sampled_peak_rss_mib": max(rss) / 1024**2 if rss else None,
        "process_read_bytes": counters["read_bytes"],
        "process_write_bytes": counters["write_bytes"],
        "device_observed": any(s.device for s in samples),
        "quality_value": None,
        "step_seconds": None,
        "throughput": None,
        "network_bytes": None,
        "recommendation_authorized": False,
        "scope": "One process thread group; sampled RSS; not child processes, model compute, "
        "GPU per-job utilization, native termination or allocation accounting.",
    }


def summary(row):
    report = row["body"]["report"]
    return {
        **{key: value for key, value in report.items() if key != "samples"},
        "assessment": row["body"]["assessment"],
        "record_digest": row["digest"],
        "imported_at": row["created_at"],
        "binding_assurance": row["body"]["binding_assurance"],
    }


def list_page(store, conn, project, page=0):
    query = select(entities).where(
        entities.c.kind == "passive_observation", entities.c.project == project
    )
    total = conn.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = conn.execute(
        query.order_by(entities.c.created_at.desc(), entities.c.ref.desc())
        .offset(page * 12)
        .limit(12)
    ).mappings()
    return {
        "items": [summary(row) for row in rows],
        "total": total,
        "page": page,
        "size": 12,
        "has_next": (page + 1) * 12 < total,
    }


class PassiveObservations:
    def __init__(self, store, *, accept_synthetic=False):
        self.store, self.accept_synthetic = store, accept_synthetic

    def create(self, project, report):
        if report.project_ref != project:
            raise Rejected("observation project must match the operator identity")
        if report.evidence_kind == "synthetic" and not self.accept_synthetic:
            raise Rejected("synthetic observations are disabled")
        if report.finished_at > now() + timedelta(seconds=5):
            raise Rejected("observation is from the future")
        with self.store.transaction() as conn:
            assurance = "operator-attested external execution and process binding"
            target = report.target
            if target.platform_job_id:
                row = self.store.job(conn, target.platform_job_id)
                if row is None or row["project"] != project:
                    raise NotFound("job not found")
                body = row["body"]
                if (
                    body["attempt_id"] != target.attempt_ref
                    or body["candidate"]["backend"] != target.backend
                    or body["backend_cluster_id"] != target.backend_cluster_id
                    or body.get("external_id") != target.external_job_ref
                    or (
                        target.backend == Backend.KUBERNETES
                        and body.get("backend_uid") != target.external_job_uid
                    )
                    or (
                        target.backend == Backend.SLURM
                        and (
                            not body.get("scheduler_submitted_at")
                            or datetime.fromisoformat(body["scheduler_submitted_at"])
                            != target.native_submitted_at
                        )
                    )
                    or body["capability"]["node_ref"] != target.node_ref
                ):
                    raise Rejected("observation does not match the persisted execution identity")
                assurance = "platform execution metadata matched; process binding operator-attested"
            row = self.store.put(
                conn,
                "passive_observation",
                report.ref,
                project,
                {
                    "report": report.model_dump(mode="json"),
                    "assessment": assessment(report),
                    "binding_assurance": assurance,
                },
            )
            return summary(row)

    def get(self, project, ref):
        with self.store.transaction() as conn:
            row = self.store.get(conn, "passive_observation", ref)
            if row is None or row["project"] != project:
                raise NotFound("observation not found")
            return {**summary(row), "samples": row["body"]["report"]["samples"]}
