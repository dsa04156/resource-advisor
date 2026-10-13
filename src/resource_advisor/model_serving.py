"""Operator-selected deployments; immutable successor edges serialize each service."""

import hashlib
import math
import re
import time

import httpx
from pydantic import Field
from sqlalchemy import select, update

from .contracts import Contract, Ref, signature
from .service import NotFound, Rejected, required
from .store import Conflict, entities, outbox


class DeploymentRequest(Contract):
    service_ref: Ref
    lifecycle_ref: Ref
    expected_deployment_ref: Ref | None = None


class RollbackRequest(Contract):
    service_ref: Ref
    target_deployment_ref: Ref
    expected_deployment_ref: Ref


class PredictionInput(Contract):
    instances: tuple[tuple[float, float], ...] = Field(min_length=1, max_length=512)


class ServingUnavailable(ValueError):
    pass


def history(store, conn, project, service):
    return list(
        conn.execute(
            select(entities)
            .where(
                entities.c.kind == "model_deployment",
                entities.c.project == project,
                entities.c.body["service_ref"].as_string() == service,
            )
            .order_by(entities.c.body["revision"].as_integer().desc())
        ).mappings()
    )


def service_name(project, ref):
    # Leave room for KServe's "-predictor-<namespace>" DNS label.
    return "ra-" + signature([project, ref])[7:31]


def lock_service(store, conn, project, service):
    ref = "msg-" + signature([project, service])[7:47]
    store.put(conn, "model_service_guard", ref, project, {"service_ref": service})
    # A no-op write locks this service row in PostgreSQL and acquires SQLite's
    # writer lock. Immutable body/digest/time stay unchanged. Policy registration
    # and deployment decisions read their fences only after this lock.
    conn.execute(
        update(entities)
        .where(entities.c.kind == "model_service_guard", entities.c.ref == ref)
        .values(ref=entities.c.ref)
    )


class ModelDeployments:
    def __init__(self, store, prediction_client=None, telemetry=None):
        self.store, self.prediction_client = store, prediction_client
        self.telemetry = telemetry

    def start(self, project, value, key, *, release_ref=None, staging=False):
        if not key or len(key) > 128:
            raise Rejected("Idempotency-Key must contain 1–128 characters")
        ref = "mdp-" + hashlib.sha256(f"{project}:{key}".encode()).hexdigest()[:40]
        operation = "rollback" if isinstance(value, RollbackRequest) else "deploy"
        request = {"operation": operation, **value.model_dump(mode="json")}
        if release_ref is not None:
            request.update(release_ref=release_ref, staging=staging)
        try:
            with self.store.transaction() as conn:
                existing = self.store.get(conn, "model_deployment", ref)
                if existing:
                    if existing["project"] != project or existing["body"]["request"] != request:
                        raise Conflict("deployment key is already bound to another command")
                else:
                    from .model_release import authorize_production, target_ref

                    lock_service(self.store, conn, project, value.service_ref)
                    decision_digest = None
                    if value.service_ref.startswith("release-stage-") and operation == "deploy":
                        if not release_ref or not staging:
                            raise Rejected("release staging services are reserved")
                        release = required(self.store, conn, "model_release", release_ref, project)
                        if (
                            value.service_ref != release["staging_service_ref"]
                            or value.lifecycle_ref != release["request"]["lifecycle_ref"]
                        ):
                            raise Conflict("staging command differs from frozen release")
                    elif operation == "deploy" and self.store.get(
                        conn, "model_release_target", target_ref(project, value.service_ref)
                    ):
                        decision_digest = authorize_production(
                            self.store, conn, project, release_ref, value
                        )
                    rows = history(self.store, conn, project, value.service_ref)
                    current = rows[0]["ref"] if rows else None
                    if value.expected_deployment_ref != current:
                        raise Conflict(
                            "expected deployment differs from the current desired deployment"
                        )
                    if operation == "rollback":
                        target = required(
                            self.store,
                            conn,
                            "model_deployment",
                            value.target_deployment_ref,
                            project,
                        )
                        ready = self.store.get(
                            conn, "model_deployment_ready", value.target_deployment_ref
                        )
                        if target["service_ref"] != value.service_ref or not ready:
                            raise Rejected("rollback requires a READY deployment from this service")
                        lifecycle_ref = target["lifecycle_ref"]
                    else:
                        lifecycle_ref = value.lifecycle_ref
                    lifecycle = required(
                        self.store, conn, "model_lifecycle", lifecycle_ref, project
                    )
                    policy = required(
                        self.store, conn, "model_lifecycle_policy", lifecycle["policy_ref"], project
                    )
                    evaluated = self.store.get(conn, "model_evaluation", lifecycle_ref)
                    if not evaluated or evaluated["project"] != project:
                        raise Rejected("deployment requires a completed quality evaluation")
                    evaluation = evaluated["body"]
                    if not evaluation["passed"] or evaluation["mse"] > policy["maximum_mse"]:
                        raise Rejected("quality-rejected models cannot be deployed")
                    registration = required(
                        self.store, conn, "model_registration", lifecycle_ref, project
                    )
                    if (
                        not evaluation["passed"]
                        or evaluation["mse"] > policy["maximum_mse"]
                        or evaluation["policy_digest"] != signature(policy)
                        or registration["evaluation_digest"] != signature(evaluation)
                        or registration["output_checkpoint_digest"]
                        != evaluation["output_checkpoint_digest"]
                    ):
                        raise Rejected(
                            "deployment requires an intact registered quality-passed lifecycle"
                        )
                    edge = "mde-" + signature([project, value.service_ref, current])[7:47]
                    self.store.put(conn, "model_deployment_edge", edge, project, {"successor": ref})
                    body = {
                        "request": request,
                        "operation": operation,
                        "service_ref": value.service_ref,
                        "lifecycle_ref": lifecycle_ref,
                        "revision": rows[0]["body"]["revision"] + 1 if rows else 1,
                        "registration": registration,
                    }
                    if release_ref is not None:
                        body.update(release_ref=release_ref, decision_digest=decision_digest)
                    self.store.put(conn, "model_deployment", ref, project, body)
                    self.store.enqueue(
                        conn,
                        ref,
                        "model_deployment",
                        {"ref": ref, "job_id": registration["training_job_id"]},
                    )
        except Conflict:
            # A concurrent retry may commit after our first absence read. Re-read
            # in a fresh transaction, including after a unique successor conflict.
            with self.store.transaction() as conn:
                existing = self.store.get(conn, "model_deployment", ref)
                if (
                    not existing
                    or existing["project"] != project
                    or existing["body"]["request"] != request
                ):
                    raise
        return self.view(project, ref)

    def view(self, project, ref):
        with self.store.transaction() as conn:
            intent = self.store.get(conn, "model_deployment", ref)
            if not intent or intent["project"] != project:
                raise NotFound("model deployment not found")
            ready = self.store.get(conn, "model_deployment_ready", ref)
            superseded = self.store.get(conn, "model_deployment_superseded", ref)
            event = conn.execute(select(outbox).where(outbox.c.id == ref)).mappings().first()
            return {
                "ref": ref,
                **intent["body"],
                "state": "READY" if ready else "SUPERSEDED" if superseded else "PENDING",
                "receipt": ready["body"] if ready else None,
                "delivery_error": event["last_error"] if event else None,
            }

    def service(self, project, ref):
        with self.store.transaction() as conn:
            rows = history(self.store, conn, project, ref)
        if not rows:
            raise NotFound("model service not found")
        views = [self.view(project, row["ref"]) for row in rows]
        return {"service_ref": ref, "desired": views[0], "history": views}

    def observe(self, project, ref):
        desired = self.service(project, ref)["desired"]
        if self.telemetry is not None:
            with self.store.transaction() as conn:
                lifecycle = required(
                    self.store, conn, "model_lifecycle", desired["lifecycle_ref"], project
                )
                policy = required(
                    self.store, conn, "model_lifecycle_policy", lifecycle["policy_ref"], project
                )
            self.telemetry.configure(project, ref, desired, policy["training_input"]["x"])
        return desired

    def observability(self, project, ref):
        desired = self.observe(project, ref)
        return {
            "service_ref": ref,
            "desired_state": desired["state"],
            **self.telemetry.snapshot(project, ref),
        }

    def observe_project(self, project):
        with self.store.transaction() as conn:
            refs = (
                conn.execute(
                    select(entities.c.body["service_ref"].as_string())
                    .where(entities.c.kind == "model_deployment", entities.c.project == project)
                    .distinct()
                )
                .scalars()
                .all()
            )
        for ref in refs:
            self.observe(project, ref)

    def probe(self, project, ref):
        desired = self.observe(project, ref)
        started = time.perf_counter()
        success, reason = False, "SERVING_UNAVAILABLE"
        try:
            with self.store.transaction() as conn:
                lifecycle = required(
                    self.store, conn, "model_lifecycle", desired["lifecycle_ref"], project
                )
                policy = required(
                    self.store, conn, "model_lifecycle_policy", lifecycle["policy_ref"], project
                )
                evaluation = required(
                    self.store, conn, "model_evaluation", desired["lifecycle_ref"], project
                )
            output = self.predict(
                project,
                ref,
                PredictionInput(instances=(policy["holdout"]["x"][0],)),
                source="probe",
            )
            if (
                output["model"]["deployment_ref"] != desired["ref"]
                or self.service(project, ref)["desired"]["ref"] != desired["ref"]
            ):
                raise Conflict("desired deployment changed while probing; retry")
            if not math.isclose(
                output["predictions"][0][0],
                evaluation["predictions"][0][0],
                rel_tol=0,
                abs_tol=1e-12,
            ):
                reason = "PREDICTION_MISMATCH"
            else:
                success, reason = True, "OK"
        except ServingUnavailable:
            pass
        except Conflict:
            reason = "DEPLOYMENT_CHANGED"
        finally:
            self.telemetry.record_probe(
                project, ref, desired["ref"], success, time.perf_counter() - started, reason
            )
        return self.observability(project, ref)

    def predict(self, project, ref, value, *, source="request"):
        started = time.perf_counter()
        desired = self.observe(project, ref)
        outcome = "error"
        try:
            result = self._predict(project, ref, value, desired)
            outcome = "success"
            return result
        except Conflict:
            outcome = "conflict"
            raise
        finally:
            if self.telemetry is not None:
                self.telemetry.record(
                    project,
                    ref,
                    desired["ref"],
                    source,
                    outcome,
                    time.perf_counter() - started,
                    value.instances,
                )

    def _predict(self, project, ref, value, desired):
        from .model_predictor import ModelDescription

        if desired["state"] != "READY":
            raise ServingUnavailable("desired model deployment has not been verified")
        receipt = desired["receipt"]
        name = service_name(project, ref)
        endpoint = receipt["endpoint"]
        if not re.fullmatch(
            r"http://" + name + r"-predictor\.[a-z0-9-]{1,63}\.svc/v1/models/" + name, endpoint
        ):
            raise ServingUnavailable("invalid configured model endpoint")
        client = self.prediction_client or httpx.Client(timeout=10)
        try:
            response = client.post(endpoint + ":predict", json=value.model_dump(mode="json"))
            response.raise_for_status()
            output = response.json()
            identity = ModelDescription.model_validate(output["model"]).model_dump(mode="json")
            if (
                any(receipt.get(k) != v for k, v in identity.items())
                or len(output["predictions"]) != len(value.instances)
                or any(len(row) != 1 or not math.isfinite(row[0]) for row in output["predictions"])
            ):
                raise ServingUnavailable(
                    "predictor identity/output differs from verified deployment"
                )
            if self.service(project, ref)["desired"]["ref"] != desired["ref"]:
                raise Conflict("desired deployment changed while predicting; retry")
            return output
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise ServingUnavailable(
                "verified model prediction is temporarily unavailable"
            ) from exc
        finally:
            if self.prediction_client is None:
                client.close()
