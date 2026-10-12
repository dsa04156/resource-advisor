"""Explicit model lifecycle intents; existing compute contracts remain unchanged."""

import hashlib
import json
import math
from pathlib import Path

from pydantic import Field, model_validator
from sqlalchemy import select

from .contracts import (
    TERMINAL,
    Contract,
    Digest,
    ExecutionResult,
    JobRequest,
    Ref,
    State,
    now,
    signature,
)
from .service import NotFound, Rejected, required
from .store import jobs, outbox


def evaluator_digest():
    directory = Path(__file__).parent
    return signature(
        {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("model_lifecycle.py", "linear_model.py", "model_registry.py")
        }
    )


class RegressionDataset(Contract):
    x: tuple[tuple[float, float], ...] = Field(min_length=1, max_length=512)
    y: tuple[tuple[float], ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def matched_rows(self):
        if len(self.x) != len(self.y) or len(set(self.x)) != len(self.x):
            raise ValueError("dataset requires matching, unique feature rows")
        return self


class LifecyclePolicy(Contract):
    ref: Ref
    project_ref: Ref
    workload_ref: Ref
    candidate_ref: Ref
    workload_digest: Digest
    model_name: Ref
    training_input: RegressionDataset
    holdout: RegressionDataset
    maximum_mse: float = Field(ge=0, le=1e12)
    evaluator_source_digest: Digest = Field(default_factory=evaluator_digest)

    @model_validator(mode="after")
    def disjoint_holdout(self):
        if set(self.training_input.x) & set(self.holdout.x):
            raise ValueError("holdout features must be absent from training input")
        if not self.model_name.startswith("ra-" + self.project_ref + "-"):
            raise ValueError("model name must use the project's ra-<project>- prefix")
        return self


class LifecycleRequest(Contract):
    policy_ref: Ref


class ModelLifecycles:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def register(self, project, policy):
        if policy.project_ref != project:
            raise Rejected("project does not match authenticated principal")
        if policy.evaluator_source_digest != evaluator_digest():
            raise Rejected("policy evaluator source digest differs from this implementation")
        with self.store.transaction() as conn:
            spec, candidate, variant, _ = self.service.bundle(
                conn, project, policy.workload_ref, policy.candidate_ref
            )
            if (
                signature(spec) != policy.workload_digest
                or spec.identity.task_type != "training"
                or spec.identity.measurement_boundary != "linear-sgd-ten-step-synchronized-v1"
                or spec.identity.input_shape != (8, 2)
                or spec.identity.work_units != 10
                or spec.identity.dataset_version != signature(policy.training_input)
                or candidate.backend != "kubernetes"
                or candidate.context.allocation_mode != "physical_device"
                or candidate.context.resources.accelerator_count != 1
                or variant.device_class != "gpu"
                or variant.verification != "TRAINING_VERIFIED"
                or variant.command != ("python", "-m", "resource_advisor.gpu_training")
            ):
                raise Rejected("policy requires the exact qualified linear GPU training workload")
            return self.store.put(
                conn, "model_lifecycle_policy", policy.ref, project, policy.model_dump(mode="json")
            )["body"]

    def start(self, project, request, key):
        if not key or len(key) > 128:
            raise Rejected("Idempotency-Key must contain 1–128 characters")
        ref = "mlc-" + hashlib.sha256(f"{project}:{key}".encode()).hexdigest()[:40]
        with self.store.transaction() as conn:
            existing = self.store.get(conn, "model_lifecycle", ref)
            policy = required(
                self.store, conn, "model_lifecycle_policy", request.policy_ref, project
            )
            accepted_job = conn.execute(
                select(jobs.c.id).where(jobs.c.project == project, jobs.c.idempotency_key == ref)
            ).first()
            if not accepted_job and policy["evaluator_source_digest"] != evaluator_digest():
                raise Rejected("policy evaluator source has changed; qualify a new policy")
            body = {"policy_ref": request.policy_ref, "policy_digest": signature(policy)}
            # Persist intent before compute acceptance. Replaying a partial start
            # resumes the same per-lifecycle Job through Service's existing key.
            self.store.put(conn, "model_lifecycle", ref, project, body)
            if existing and existing["body"] != body:
                raise Rejected("lifecycle key is already bound to another policy")
        job = self.service.submit(
            project,
            JobRequest(workload_ref=policy["workload_ref"], candidate_ref=policy["candidate_ref"]),
            ref,
        )
        with self.store.transaction() as conn:
            self.store.enqueue(conn, ref, "model_lifecycle", {"ref": ref, "job_id": job["job_id"]})
        return self.view(project, ref)

    def view(self, project, ref):
        with self.store.transaction() as conn:
            row = self.store.get(conn, "model_lifecycle", ref)
            if not row or row["project"] != project:
                raise NotFound("model lifecycle not found")
            policy = required(
                self.store, conn, "model_lifecycle_policy", row["body"]["policy_ref"], project
            )
            job = (
                conn.execute(
                    select(jobs).where(jobs.c.project == project, jobs.c.idempotency_key == ref)
                )
                .mappings()
                .first()
            )
            evaluation = self.store.get(conn, "model_evaluation", ref)
            registered = self.store.get(conn, "model_registration", ref)
            event = conn.execute(select(outbox).where(outbox.c.id == ref)).mappings().first()
            state = "TRAINING" if job else "SUBMISSION_PENDING"
            if job and job["state"] in {State.FAILED, State.CANCELED, State.RESULT_INVALID}:
                state = "TRAINING_FAILED"
            elif job and job["state"] == State.SUCCEEDED:
                state = "WAITING_FOR_ARTIFACT"
            if evaluation:
                state = (
                    "REGISTRATION_PENDING" if evaluation["body"]["passed"] else "QUALITY_REJECTED"
                )
            if registered:
                state = "REGISTERED"
            error = event["last_error"] if event else None
            if error == "REGISTRY_RECONCILIATION_REQUIRED" and not registered:
                state = error
            return {
                "ref": ref,
                "state": state,
                "created_at": row["created_at"],
                **row["body"],
                "model_name": policy["model_name"],
                "job_id": job["id"] if job else None,
                "attempt_id": job["body"]["attempt_id"] if job else None,
                "training_state": job["state"] if job else None,
                "evaluation": evaluation["body"] if evaluation else None,
                "registration": registered["body"] if registered else None,
                "delivery_error": error,
            }


class LifecyclePending(ValueError):
    pass


class LifecycleDelivery:
    def __init__(self, service, storage, registry, *, job_routes=None):
        self.service, self.store = service, service.store
        self.storage, self.registry, self.job_routes = storage, registry, job_routes

    def deliver_one(self):
        import httpx
        from botocore.exceptions import BotoCoreError, ClientError

        event = self.store.claim("model_lifecycle", job_routes=self.job_routes)
        if not event:
            return False
        try:
            self.advance(event)
            self.store.finish(event)
        except (
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
            BotoCoreError,
            ClientError,
            httpx.HTTPError,
        ) as exc:
            error = str(exc) if isinstance(exc, LifecyclePending) else type(exc).__name__
            self.store.finish(event, error)
        return True

    def advance(self, event):
        from .training import validate_receipt

        ref = event["body"]["ref"]
        with self.store.transaction() as conn:
            intent = self.store.get(conn, "model_lifecycle", ref)
            row = self.store.job(conn, event["body"]["job_id"])
            if (
                not intent
                or not row
                or row["project"] != intent["project"]
                or row["idempotency_key"] != ref
            ):
                raise LifecyclePending("LIFECYCLE_OWNERSHIP_MISMATCH")
            project, job = row["project"], row["body"]
            policy = LifecyclePolicy.model_validate(
                required(
                    self.store,
                    conn,
                    "model_lifecycle_policy",
                    intent["body"]["policy_ref"],
                    project,
                )
            )
            if signature(policy) != intent["body"]["policy_digest"]:
                raise LifecyclePending("LIFECYCLE_POLICY_CHANGED")
            if policy.evaluator_source_digest != evaluator_digest():
                raise LifecyclePending("EVALUATOR_SOURCE_CHANGED")
            if row["state"] in TERMINAL and row["state"] != State.SUCCEEDED:
                return
            if row["state"] != State.SUCCEEDED:
                raise LifecyclePending("TRAINING_NOT_COMPLETE")
            artifact = self.store.get(conn, "artifact", "artifact-" + job["attempt_id"])
            saved_receipt = self.store.get(conn, "training_receipt", job["attempt_id"])
            evaluation = self.store.get(conn, "model_evaluation", ref)
        if not artifact:
            raise LifecyclePending("TRAINING_ARTIFACT_NOT_READY")
        bundle = json.loads(self.storage.read(artifact["body"]))
        result = ExecutionResult.model_validate(bundle["result"])
        receipt = validate_receipt(bundle["training_receipt"], result, job["training_isolation"])
        if (
            artifact["project"] != project
            or result.evidence_kind != "hardware"
            or result.job_id != row["id"]
            or result.attempt_id != job["attempt_id"]
            or signature(result) != job["result_digest"]
            or not job["quality_passed"]
            or signature(bundle["workload"]) != policy.workload_digest
            or bundle["candidate"] != job["candidate"]
            or bundle["variant"] != job["variant"]
            or bundle["mode"] != "observe"
            or not saved_receipt
            or saved_receipt["body"] != receipt.model_dump(mode="json")
        ):
            raise LifecyclePending("QUALIFIED_TRAINING_ARTIFACT_REQUIRED")
        if not evaluation:
            from .linear_model import LinearModel

            predictions = LinearModel(receipt.output_checkpoint).predict(policy.holdout.x)
            mse = math.fsum(
                (p[0] - y[0]) ** 2 for p, y in zip(predictions, policy.holdout.y, strict=True)
            ) / len(predictions)
            if not math.isfinite(mse):
                raise LifecyclePending("NONFINITE_EVALUATION")
            evaluated = {
                "lifecycle_ref": ref,
                "policy_digest": signature(policy),
                "training_job_id": row["id"],
                "training_attempt_id": job["attempt_id"],
                "code_digest": job["spec"]["identity"]["code_digest"],
                "training_input_digest": receipt.input_digest,
                "initial_checkpoint_digest": receipt.initial_checkpoint_digest,
                "output_checkpoint_digest": receipt.output_checkpoint_digest,
                "artifact_ref": artifact["ref"],
                "artifact_digest": artifact["body"]["digest"],
                "holdout_digest": signature(policy.holdout),
                "evaluator": "linear-regression-mse-v1",
                "evaluator_source_digest": policy.evaluator_source_digest,
                "evaluated_at": now().isoformat(),
                "sample_count": len(predictions),
                "predictions": predictions,
                "mse": mse,
                "maximum_mse": policy.maximum_mse,
                "passed": mse <= policy.maximum_mse,
            }
            with self.store.transaction() as conn:
                evaluation = self.store.put(conn, "model_evaluation", ref, project, evaluated)
        if not evaluation["body"]["passed"]:
            return
        if self.registry is None:
            raise LifecyclePending("MODEL_REGISTRY_NOT_CONFIGURED")
        registered = self.registry.register(
            ref, row, policy, evaluation["body"], receipt.output_checkpoint, event
        )
        with self.store.transaction() as conn:
            self.store.put(conn, "model_registration", ref, project, registered)
