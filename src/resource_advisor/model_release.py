"""Frozen candidates, staging evidence and operator decisions for model promotion."""

import hashlib
import math
import time
from datetime import datetime
from typing import Literal

from pydantic import Field

from .contracts import Contract, Digest, Ref, now, signature
from .model_serving import (
    DeploymentRequest,
    PredictionInput,
    ServingUnavailable,
    history,
    lock_service,
)
from .service import NotFound, Rejected, required
from .store import Conflict


class ReleasePolicy(Contract):
    ref: Ref
    project_ref: Ref
    lifecycle_policy_ref: Ref
    production_service_ref: Ref
    expected_production_deployment_ref: Ref | None = None
    maximum_mse_regression: float = Field(ge=0, le=1e12)
    maximum_probe_latency_ms: float = Field(gt=0, le=60000)
    verification_ttl_seconds: int = Field(ge=60, le=86400)


class ReleaseRequest(Contract):
    policy_ref: Ref
    lifecycle_ref: Ref


class ReleaseDecision(Contract):
    decision: Literal["approve", "reject"]
    verification_digest: Digest
    reason: str = Field(min_length=1, max_length=1000)


def target_ref(project, service):
    return "mrt-" + signature([project, service])[7:47]


def checked_evidence(store, conn, project, ref):
    release = required(store, conn, "model_release", ref, project)
    verified = store.get(conn, "model_release_verification", ref)
    if not verified or verified["project"] != project:
        raise Rejected("release requires completed staging verification")
    evidence = verified["body"]
    policy = release["policy"]["request"]
    age = (now() - datetime.fromisoformat(evidence["verified_at"])).total_seconds()
    if not 0 <= age <= policy["verification_ttl_seconds"]:
        raise Rejected("staging verification has expired; create a new candidate")
    staged = history(store, conn, project, release["staging_service_ref"])
    if not staged or staged[0]["ref"] != evidence["staging_deployment_ref"]:
        raise Conflict("staging deployment changed after verification")
    ready = required(store, conn, "model_deployment_ready", staged[0]["ref"], project)
    if signature(ready) != evidence["staging_receipt_digest"]:
        raise Conflict("staging receipt changed after verification")
    production = history(store, conn, project, policy["production_service_ref"])
    if (production[0]["ref"] if production else None) != policy[
        "expected_production_deployment_ref"
    ]:
        raise Conflict("production baseline changed; qualify a new release policy")
    return release, evidence


def authorize_production(store, conn, project, ref, value):
    if ref is None:
        raise Rejected("protected production service requires an approved release")
    decided = store.get(conn, "model_release_decision", ref)
    if not decided or decided["project"] != project:
        raise Rejected("release requires an operator approval")
    decision = decided["body"]
    if decision["request"]["decision"] != "approve":
        raise Rejected("release was rejected")
    release, evidence = checked_evidence(store, conn, project, ref)
    if (
        decision["request"]["verification_digest"] != signature(evidence)
        or value.service_ref != release["policy"]["request"]["production_service_ref"]
        or value.lifecycle_ref != release["request"]["lifecycle_ref"]
        or value.expected_deployment_ref
        != release["policy"]["request"]["expected_production_deployment_ref"]
    ):
        raise Conflict("deployment differs from exact approved release")
    return signature(decision)


class ModelReleases:
    def __init__(self, store, deployments):
        self.store, self.deployments = store, deployments

    def register(self, project, policy):
        if policy.project_ref != project:
            raise Rejected("project does not match authenticated principal")
        if policy.production_service_ref.startswith("release-stage-"):
            raise Rejected("staging service names are reserved")
        with self.store.transaction() as conn:
            existing = self.store.get(conn, "model_release_policy", policy.ref)
            request = policy.model_dump(mode="json")
            if existing:
                if existing["project"] != project or existing["body"]["request"] != request:
                    raise Conflict("release policy is immutable")
                return existing["body"]
            required(
                self.store, conn, "model_lifecycle_policy", policy.lifecycle_policy_ref, project
            )
            lock_service(self.store, conn, project, policy.production_service_ref)
            baseline = self.baseline(
                conn,
                project,
                policy.production_service_ref,
                policy.expected_production_deployment_ref,
            )
            body = {"request": request, "baseline": baseline}
            self.store.put(conn, "model_release_policy", policy.ref, project, body)
            self.store.put(
                conn,
                "model_release_target",
                target_ref(project, policy.production_service_ref),
                project,
                {"service_ref": policy.production_service_ref},
            )
            return body

    def baseline(self, conn, project, service, expected):
        rows = history(self.store, conn, project, service)
        if (rows[0]["ref"] if rows else None) != expected:
            raise Conflict("release policy baseline differs from current desired deployment")
        if not rows:
            return None
        row = rows[0]
        ready = required(self.store, conn, "model_deployment_ready", row["ref"], project)
        evaluation = required(
            self.store, conn, "model_evaluation", row["body"]["lifecycle_ref"], project
        )
        return {"ref": row["ref"], "receipt": ready, "evaluation": evaluation}

    def start(self, project, request, key):
        if not key or len(key) > 128:
            raise Rejected("Idempotency-Key must contain 1–128 characters")
        ref = "mrl-" + hashlib.sha256(f"{project}:{key}".encode()).hexdigest()[:40]
        with self.store.transaction() as conn:
            existing = self.store.get(conn, "model_release", ref)
            value = request.model_dump(mode="json")
            if existing:
                if existing["project"] != project or existing["body"]["request"] != value:
                    raise Conflict("release key is already bound to another candidate")
            else:
                policy = required(
                    self.store, conn, "model_release_policy", request.policy_ref, project
                )
                lifecycle = required(
                    self.store, conn, "model_lifecycle", request.lifecycle_ref, project
                )
                policy_record = self.store.get(conn, "model_release_policy", request.policy_ref)
                lifecycle_record = self.store.get(conn, "model_lifecycle", request.lifecycle_ref)
                if datetime.fromisoformat(lifecycle_record["created_at"]) <= datetime.fromisoformat(
                    policy_record["created_at"]
                ):
                    raise Rejected("release policy must be frozen before candidate training starts")
                if lifecycle["policy_ref"] != policy["request"]["lifecycle_policy_ref"]:
                    raise Rejected("candidate does not use the frozen lifecycle policy")
                evaluation = required(
                    self.store, conn, "model_evaluation", request.lifecycle_ref, project
                )
                registration = required(
                    self.store, conn, "model_registration", request.lifecycle_ref, project
                )
                if not evaluation["passed"] or registration["evaluation_digest"] != signature(
                    evaluation
                ):
                    raise Rejected("release requires an intact registered quality-passed model")
                baseline = policy["baseline"]
                baseline_mse = baseline["evaluation"]["mse"] if baseline else None
                if baseline and any(
                    evaluation[k] != baseline["evaluation"][k]
                    for k in ("holdout_digest", "evaluator", "evaluator_source_digest")
                ):
                    raise Rejected("candidate and baseline evaluations are not comparable")
                if (
                    baseline
                    and evaluation["mse"]
                    > baseline_mse + policy["request"]["maximum_mse_regression"]
                ):
                    raise Rejected("candidate exceeds the frozen baseline regression limit")
                body = {
                    "request": value,
                    "policy": policy,
                    "registration": registration,
                    "evaluation_digest": signature(evaluation),
                    "staging_service_ref": "release-stage-" + ref[4:],
                    "comparison": {
                        "candidate_mse": evaluation["mse"],
                        "baseline_mse": baseline_mse,
                    },
                }
                self.store.put(conn, "model_release", ref, project, body)
        return self.view(project, ref)

    def view(self, project, ref):
        with self.store.transaction() as conn:
            row = self.store.get(conn, "model_release", ref)
            if not row or row["project"] != project:
                raise NotFound("model release not found")
            staged = self.store.get(conn, "model_release_stage", ref)
            verified = self.store.get(conn, "model_release_verification", ref)
            decided = self.store.get(conn, "model_release_decision", ref)
            promoted = self.store.get(conn, "model_release_deployment", ref)
            blocker = None
            current_production = None
            if verified and not promoted:
                try:
                    checked_evidence(self.store, conn, project, ref)
                except (Rejected, Conflict) as exc:
                    blocker = str(exc)
            if promoted:
                production = history(
                    self.store,
                    conn,
                    project,
                    row["body"]["policy"]["request"]["production_service_ref"],
                )
                current_production = production[0]["ref"] if production else None
        staging = (
            self.deployments.view(project, staged["body"]["deployment_ref"]) if staged else None
        )
        deployment = (
            self.deployments.view(project, promoted["body"]["deployment_ref"]) if promoted else None
        )
        state = (
            "PENDING_APPROVAL"
            if verified
            else "STAGED"
            if staging and staging["state"] == "READY"
            else "STAGING"
            if staging
            else "CANDIDATE"
        )
        if decided:
            state = (
                "APPROVED" if decided["body"]["request"]["decision"] == "approve" else "REJECTED"
            )
        if deployment:
            state = (
                "DEPLOYED"
                if deployment["state"] == "READY"
                else "SUPERSEDED"
                if deployment["state"] == "SUPERSEDED"
                else "DEPLOYING"
            )
            if current_production != deployment["ref"]:
                state = "SUPERSEDED"
        return {
            "ref": ref,
            "created_at": row["created_at"],
            **row["body"],
            "state": state,
            "staging": staging,
            "verification": verified["body"] if verified else None,
            "decision": decided["body"] if decided else None,
            "deployment": deployment,
            "promotion_blocker": blocker,
        }

    def stage(self, project, ref):
        release = self.view(project, ref)
        deployed = self.deployments.start(
            project,
            DeploymentRequest(
                service_ref=release["staging_service_ref"],
                lifecycle_ref=release["request"]["lifecycle_ref"],
            ),
            "release-stage-" + ref,
            release_ref=ref,
            staging=True,
        )
        with self.store.transaction() as conn:
            self.store.put(
                conn, "model_release_stage", ref, project, {"deployment_ref": deployed["ref"]}
            )
        return self.view(project, ref)

    def decide(self, project, ref, value, actor):
        from .configuration import valid_ref

        if not valid_ref(actor) or not value.reason.strip():
            raise Rejected("configured operator subject and nonempty reason required")
        request = value.model_dump(mode="json")
        with self.store.transaction() as conn:
            required(self.store, conn, "model_release", ref, project)
            existing = self.store.get(conn, "model_release_decision", ref)
        if existing:
            if existing["body"]["request"] != request or existing["body"]["actor"] != actor:
                raise Conflict("release decision is immutable")
            return self.view(project, ref)
        self.checked_stage(project, self.view(project, ref))
        try:
            with self.store.transaction() as conn:
                _, evidence = checked_evidence(self.store, conn, project, ref)
                if value.verification_digest != signature(evidence):
                    raise Conflict("decision must reference the exact staging verification")
                self.store.put(
                    conn,
                    "model_release_decision",
                    ref,
                    project,
                    {"request": request, "actor": actor, "decided_at": now().isoformat()},
                )
        except Conflict:
            with self.store.transaction() as conn:
                saved = self.store.get(conn, "model_release_decision", ref)
                if (
                    not saved
                    or saved["body"]["request"] != request
                    or saved["body"]["actor"] != actor
                ):
                    raise
        return self.view(project, ref)

    def deploy(self, project, ref):
        release = self.view(project, ref)
        policy = release["policy"]["request"]
        deployed = self.deployments.start(
            project,
            DeploymentRequest(
                service_ref=policy["production_service_ref"],
                lifecycle_ref=release["request"]["lifecycle_ref"],
                expected_deployment_ref=policy["expected_production_deployment_ref"],
            ),
            "release-production-" + ref,
            release_ref=ref,
        )
        with self.store.transaction() as conn:
            self.store.put(
                conn, "model_release_deployment", ref, project, {"deployment_ref": deployed["ref"]}
            )
        return self.view(project, ref)

    def checked_stage(self, project, release):
        stage = release["staging"]
        if not stage or stage["state"] != "READY":
            raise ServingUnavailable("staging deployment has not been verified")
        if (
            self.deployments.service(project, release["staging_service_ref"])["desired"]["ref"]
            != stage["ref"]
        ):
            raise Conflict("staging deployment changed")
        if any(
            stage["receipt"][k] != release["registration"][k]
            for k in ("model_uri", "model_package_digest", "output_checkpoint_digest")
        ):
            raise Conflict("staging identity differs from frozen candidate")
        return stage

    def verify(self, project, ref):
        release = self.view(project, ref)
        stage = self.checked_stage(project, release)
        if release["verification"]:
            return release
        with self.store.transaction() as conn:
            evaluation = required(
                self.store, conn, "model_evaluation", release["request"]["lifecycle_ref"], project
            )
            policy = required(
                self.store,
                conn,
                "model_lifecycle_policy",
                release["policy"]["request"]["lifecycle_policy_ref"],
                project,
            )
        if signature(evaluation) != release["evaluation_digest"]:
            raise Conflict("candidate evaluation changed")
        probes = []
        for _ in range(3):
            started = time.monotonic()
            output = self.deployments.predict(
                project,
                release["staging_service_ref"],
                PredictionInput(instances=policy["holdout"]["x"]),
            )
            elapsed = (time.monotonic() - started) * 1000
            error = max(
                abs(actual[0] - expected[0])
                for actual, expected in zip(
                    output["predictions"], evaluation["predictions"], strict=True
                )
            )
            if (
                output["model"]["deployment_ref"] != stage["ref"]
                or not math.isfinite(error)
                or error > 1e-12
            ):
                raise Rejected("staging predictions differ from frozen evaluation")
            if elapsed > release["policy"]["request"]["maximum_probe_latency_ms"]:
                raise Rejected("staging probe exceeds frozen latency ceiling")
            probes.append(
                {
                    "latency_ms": elapsed,
                    "prediction_count": len(output["predictions"]),
                    "maximum_abs_error": error,
                }
            )
        self.checked_stage(project, self.view(project, ref))
        body = {
            "release_ref": ref,
            "staging_deployment_ref": stage["ref"],
            "staging_receipt_digest": signature(stage["receipt"]),
            "model_uri": release["registration"]["model_uri"],
            "model_package_digest": release["registration"]["model_package_digest"],
            "evaluation_digest": release["evaluation_digest"],
            "verified_at": now().isoformat(),
            "probes": probes,
        }
        try:
            with self.store.transaction() as conn:
                self.store.put(conn, "model_release_verification", ref, project, body)
        except Conflict:
            # The first concurrent valid observation is the immutable evidence.
            with self.store.transaction() as conn:
                saved = required(self.store, conn, "model_release_verification", ref, project)
                if saved["staging_receipt_digest"] != body["staging_receipt_digest"]:
                    raise
        return self.view(project, ref)

    def listing(self, project):
        with self.store.transaction() as conn:
            rows = sorted(
                self.store.list(conn, "model_release", project),
                key=lambda x: x["created_at"],
                reverse=True,
            )
        return {"items": [self.view(project, row["ref"]) for row in rows[:100]]}
