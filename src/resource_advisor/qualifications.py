"""Immutable, operator-imported classification evidence; never grants execution rights."""

import math
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .contracts import Contract, Digest, Ref, now, signature
from .service import NotFound, Rejected
from .store import Conflict, entities

ClassIndex = Annotated[int, Field(strict=True, ge=0, lt=1000)]
PositiveSeconds = Annotated[float, Field(gt=0, le=86400)]


class ClassificationSample(Contract):
    input_digest: Digest
    output_digest: Digest
    label: ClassIndex
    reference_top1: ClassIndex
    prediction: ClassIndex
    elapsed_seconds: PositiveSeconds


class ClassificationGates(Contract):
    minimum_accuracy: float = Field(ge=0, le=1)
    minimum_reference_agreement: float = Field(ge=0, le=1)
    maximum_accuracy_loss: float = Field(ge=0, le=1)


class QualificationImport(Contract):
    ref: Ref
    project_ref: Ref
    kind: Literal["classification-qualification-v1"] = "classification-qualification-v1"
    provenance: Literal["operator_import"] = "operator_import"
    evidence_kind: Literal["hardware", "synthetic"]
    model_name: str = Field(min_length=1, max_length=128)
    accelerator_model: str = Field(min_length=1, max_length=128)
    device_class: Literal["gpu", "npu", "cpu"]
    model_digest: Digest
    compiled_artifact_digest: Digest | None = None
    image_digest: Digest
    runner_digest: Digest
    input_manifest_digest: Digest
    source_report_digest: Digest
    plan_ref: Ref
    runtime_versions: dict[str, str] = Field(max_length=20)
    backend: Literal["kubernetes", "slurm"]
    external_job_ref: Ref
    external_job_uid: Ref
    started_at: AwareDatetime
    finished_at: AwareDatetime
    container_exit_code: int = Field(strict=True, ge=0, le=255)
    measurement_boundary: str = Field(min_length=1, max_length=256)
    host_process_peak_rss_mib: float = Field(ge=0)
    scheduled_to_container_finished_seconds: float | None = Field(default=None, ge=0)
    admission_to_workload_finished_seconds: float | None = Field(default=None, ge=0)
    expected_images: int = Field(strict=True, ge=1, le=1000)
    gates: ClassificationGates
    samples: tuple[ClassificationSample, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def complete_classification_evidence(self):
        if len(self.samples) != self.expected_images:
            raise ValueError("one result per expected image is required")
        if len({s.input_digest for s in self.samples}) != self.expected_images:
            raise ValueError("repeated input tensors cannot inflate the accuracy sample")
        if self.finished_at < self.started_at:
            raise ValueError("execution timestamps are reversed")
        if any(
            not k or len(k) > 64 or not v or len(v) > 128 for k, v in self.runtime_versions.items()
        ):
            raise ValueError("runtime version keys and values must be bounded nonempty strings")
        return self


def assessment(value):
    """Compute gates from individual predictions, never accept a caller's verdict."""
    count = len(value.samples)
    accuracy = sum(s.prediction == s.label for s in value.samples) / count
    reference = sum(s.reference_top1 == s.label for s in value.samples) / count
    agreement = sum(s.prediction == s.reference_top1 for s in value.samples) / count
    loss = reference - accuracy
    gates = value.gates
    checks = {
        "minimum_accuracy": accuracy >= gates.minimum_accuracy,
        "minimum_reference_agreement": agreement >= gates.minimum_reference_agreement,
        "maximum_accuracy_loss": loss <= gates.maximum_accuracy_loss + 1e-12,
    }
    durations = sorted(s.elapsed_seconds for s in value.samples)
    elapsed = sum(durations)
    return {
        "sample_count": count,
        "accuracy": accuracy,
        "reference_accuracy": reference,
        "reference_agreement": agreement,
        "accuracy_loss": loss,
        "checks": checks,
        "quality_passed": all(checks.values()),
        "failed_checks": [k for k, passed in checks.items() if not passed],
        "inference_completed": True,
        "elapsed_seconds": elapsed,
        "latency_p95_ms": durations[math.ceil(count * 0.95) - 1] * 1000,
        "throughput_images_per_second": count / elapsed,
        "recommendation_authorized": False,
    }


def summary(row):
    body = row["body"]
    return {
        **{k: v for k, v in body["report"].items() if k != "samples"},
        "assessment": body["assessment"],
        "record_digest": row["digest"],
        "imported_at": row["created_at"],
        "assurance": "Operator-attested external execution; server recomputed sample metrics."
        " Import does not authorize a RuntimeVariant or add scheduler usage.",
    }


def list_page(store, conn, project, page=0):
    query = select(entities).where(
        entities.c.kind == "qualification", entities.c.project == project
    )
    total = conn.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = conn.execute(
        query.order_by(entities.c.created_at.desc(), entities.c.ref.desc())
        .offset(page * 25)
        .limit(25)
    ).mappings()
    return {
        "page": page,
        "size": 25,
        "total": total,
        "has_next": (page + 1) * 25 < total,
        "items": [summary(row) for row in rows],
    }


class Qualifications:
    def __init__(self, store):
        self.store = store

    def create(self, project, value):
        if value.project_ref != project:
            raise Rejected("project does not match authenticated principal")
        if value.finished_at > now() + timedelta(seconds=5):
            raise Rejected("future execution evidence cannot be imported")
        body = {"report": value.model_dump(mode="json"), "assessment": assessment(value)}
        try:
            with self.store.transaction() as conn:
                row = self.store.put(conn, "qualification", value.ref, project, body)
                return summary(row)
        except IntegrityError:
            # Concurrent identical imports are idempotent; different reports must not overwrite.
            with self.store.transaction() as conn:
                row = self.store.get(conn, "qualification", value.ref)
                if row and row["project"] == project and row["digest"] == signature(body):
                    return summary(row)
            raise Conflict("immutable qualification reference conflict") from None

    def get(self, project, ref):
        with self.store.transaction() as conn:
            row = self.store.get(conn, "qualification", ref)
            if row is None or row["project"] != project:
                raise NotFound("qualification not found")
            return {**summary(row), "samples": row["body"]["report"]["samples"]}
