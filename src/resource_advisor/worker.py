"""Durable worker; uncertain submission is reconciled, never blindly repeated."""

from datetime import datetime

import httpx
from sqlalchemy import select

from .backends import BackendError, SubmissionUnknown
from .contracts import TERMINAL, ExecutionResult, State, now, signature
from .policy import compatibility
from .service import Rejected
from .store import Conflict, jobs


class Worker:
    def __init__(self, service, backends):
        self.service, self.store, self.backends = service, service.store, backends

    def backend(self, row):
        # Keys include project and cluster: clients cannot route into another project queue.
        key = (row["project"], row["body"]["backend_cluster_id"])
        if key not in self.backends:
            raise BackendError("backend is not enabled for this project/cluster")
        return self.backends[key]

    def _row(self, job_id):
        with self.store.transaction() as conn:
            return self.store.job(conn, job_id)

    def submit_one(self):
        event = self.store.claim("submit")
        if not event:
            return False
        try:
            row = self._row(event["body"]["job_id"])
            if row["state"] in TERMINAL or row["state"] == State.CANCEL_REQUESTED:
                self.store.finish(event)
                return True
            backend = self.backend(row)
            if row["state"] in {State.SUBMITTING, State.SUBMISSION_UNKNOWN}:
                external = backend.reconcile(row)
                if not external:
                    raise SubmissionUnknown(
                        "no matching external job yet; operator resolution required"
                    )
            elif row["state"] == State.VALIDATED:
                with self.store.transaction() as conn:
                    args = self.service.bundle(
                        conn,
                        row["project"],
                        row["body"]["spec"]["ref"],
                        row["body"]["candidate"]["ref"],
                    )
                    errors = compatibility(*args)
                    if row["body"].get("deadline_at") and now() >= datetime.fromisoformat(
                        row["body"]["deadline_at"]
                    ):
                        errors.append("PROBE_DEADLINE")
                    if errors:
                        self.store.change_job(
                            conn,
                            row,
                            State.FAILED,
                            dict(row["body"], error="PRE_SUBMISSION:" + ",".join(errors)),
                        )
                if errors:
                    self.store.finish(event)
                    return True
                # Commit intent before the remote call. A crash now must reconcile.
                with self.store.transaction() as conn:
                    self.store.change_job(conn, row, State.SUBMITTING, row["body"])
                external = backend.submit(row)
            else:
                self.store.finish(event)
                return True
            with self.store.transaction() as conn:
                latest = self.store.job(conn, row["id"])
                body = dict(latest["body"], external_id=external, queued_at=now().isoformat())
                target = (
                    State.CANCEL_REQUESTED
                    if latest["state"] == State.CANCEL_REQUESTED
                    else State.QUEUED
                )
                self.store.change_job(conn, latest, target, body)
            self.store.finish(event)
        except (BackendError, Conflict, ValueError) as exc:
            # Exceptions after intent can be transport failures or malformed successful responses.
            with self.store.transaction() as conn:
                row = self.store.job(conn, event["body"]["job_id"])
                if row["state"] == State.SUBMITTING:
                    self.store.change_job(
                        conn,
                        row,
                        State.SUBMISSION_UNKNOWN,
                        dict(row["body"], error="SUBMISSION_RESPONSE_UNCERTAIN"),
                    )
            self.store.finish(event, type(exc).__name__)
        return True

    def cancel_one(self):
        event = self.store.claim("cancel")
        if not event:
            return False
        try:
            row = self._row(event["body"]["job_id"])
            if row["state"] in TERMINAL:
                self.store.finish(event)
                return True
            if row["body"].get("cancel_before_submit"):
                with self.store.transaction() as conn:
                    self.store.change_job(conn, row, State.CANCELED, row["body"])
                self.store.finish(event)
                return True
            if not row["body"].get("external_id"):
                # Never mark a possibly submitted job cancelled without remote confirmation.
                external = self.backend(row).reconcile(row)
                if not external:
                    raise SubmissionUnknown("cannot confirm absence after cancellation")
                with self.store.transaction() as conn:
                    self.store.change_job(
                        conn, row, State.CANCEL_REQUESTED, dict(row["body"], external_id=external)
                    )
                row = self._row(row["id"])
            self.backend(row).cancel(row)
            self.store.finish(event)
        except (BackendError, Conflict, ValueError) as exc:
            self.store.finish(event, type(exc).__name__)
        return True

    def reconcile_all(self):
        with self.store.transaction() as conn:
            active = list(
                conn.execute(
                    select(jobs).where(
                        jobs.c.state.in_(
                            [State.QUEUED, State.RUNNING, State.COLLECTING, State.CANCEL_REQUESTED]
                        )
                    )
                ).mappings()
            )
        for row in active:
            try:
                if not row["body"].get("external_id"):
                    continue
                backend = self.backend(row)
                observation = backend.status(row)
                with self.store.transaction() as conn:
                    latest = self.store.job(conn, row["id"])
                    if latest["state"] in TERMINAL:
                        continue
                    body = dict(latest["body"])
                    for key, value in [
                        ("started_at", observation.started_at),
                        ("backend_finished_at", observation.finished_at),
                        ("error", observation.error),
                    ]:
                        if value:
                            body[key] = value
                    state = observation.state
                    limits = body.get("execution_limits", body["spec"]["execution"])
                    if state == State.COLLECTING:
                        body.setdefault("collecting_since", now().isoformat())
                        collection_age = (
                            now() - datetime.fromisoformat(body["collecting_since"])
                        ).total_seconds()
                        if collection_age > limits["max_collection_seconds"]:
                            state, body["error"] = (
                                State.RESULT_INVALID,
                                "RESULT_COLLECTION_DEADLINE",
                            )
                    if latest["state"] == State.CANCEL_REQUESTED and state not in TERMINAL:
                        state = State.CANCEL_REQUESTED
                    if state == State.QUEUED and body.get("queued_at"):
                        wait = (now() - datetime.fromisoformat(body["queued_at"])).total_seconds()
                        if wait > limits["max_queue_seconds"]:
                            state, body["error"] = State.CANCEL_REQUESTED, "QUEUE_DEADLINE_EXCEEDED"
                            self.store.enqueue(
                                conn,
                                "cancel-" + body["attempt_id"],
                                "cancel",
                                {"job_id": row["id"]},
                            )
                    if (
                        body.get("deadline_at")
                        and now() >= datetime.fromisoformat(body["deadline_at"])
                        and state not in TERMINAL
                    ):
                        state, body["error"] = State.CANCEL_REQUESTED, "PROBE_WALL_DEADLINE"
                        self.store.enqueue(
                            conn, "cancel-" + body["attempt_id"], "cancel", {"job_id": row["id"]}
                        )
                    self.store.change_job(conn, latest, state, body)
                if state == State.COLLECTING:
                    envelope = backend.result(row)
                    result = ExecutionResult.model_validate(envelope["result"])
                    self.service.ingest(row["project"], result, envelope["digest"])
            except (BackendError, Conflict, Rejected, ValueError, KeyError) as exc:
                # Transport errors are observable without falsely failing a running workload.
                try:
                    with self.store.transaction() as conn:
                        latest = self.store.job(conn, row["id"])
                        if latest["state"] not in TERMINAL:
                            self.store.change_job(
                                conn,
                                latest,
                                latest["state"],
                                dict(
                                    latest["body"],
                                    last_observation_error=type(exc).__name__,
                                    last_observation_error_at=now().isoformat(),
                                ),
                            )
                except Conflict:
                    pass  # Another worker already changed this version.


class MLflowDelivery:
    def __init__(self, store, url, *, experiments, token=None, client=None):
        import httpx

        self.store = store
        if not experiments or any(
            not isinstance(v, str) or not v.strip() for v in experiments.values()
        ):
            raise ValueError("explicit project-to-MLflow-experiment mapping required")
        self.experiments = dict(experiments)
        self.client = client or httpx.Client(
            base_url=url.rstrip("/"),
            timeout=15,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    def post(self, path, body):
        response = self.client.post("/api/2.0/mlflow/" + path, json=body)
        response.raise_for_status()
        return response.json()

    def deliver_one(self):
        event = self.store.claim("mlflow")
        if not event:
            return False
        try:
            b = event["body"]
            result = b["result"]
            experiment_id = self.experiments[b["project"]]
            attempt = result["attempt_id"]
            with self.store.transaction() as conn:
                job = self.store.job(conn, b["job_id"])["body"]
            start_time = int(
                datetime.fromisoformat(job.get("started_at") or job["created_at"]).timestamp()
                * 1000
            )
            timestamp = int(datetime.fromisoformat(job["finished_at"]).timestamp() * 1000)
            search = self.post(
                "runs/search",
                {
                    "experiment_ids": [experiment_id],
                    "filter": f"tags.`resource_advisor.attempt_id` = '{attempt}'",
                    "max_results": 2,
                },
            )
            found = search.get("runs", [])
            if len(found) > 1:
                raise ValueError("duplicate external MLflow run requires reconciliation")
            tags = {
                "resource_advisor.attempt_id": attempt,
                "resource_advisor.job_id": b["job_id"],
                "project": b["project"],
                "evidence_kind": result["evidence_kind"],
                "workload_signature": result["workload_signature"],
                "context_signature": result["context_signature"],
                "result_digest": signature(result),
                "quality_passed": str(b["quality_passed"]).lower(),
                "execution_mode": job["request"]["mode"],
            }
            if job["request"].get("study_ref"):
                tags["resource_advisor.study_id"] = job["request"]["study_ref"]
            if b.get("parent_run_ref"):
                tags["mlflow.parentRunId"] = b["parent_run_ref"]
            run = (
                found[0]
                if found
                else self.post(
                    "runs/create",
                    {
                        "experiment_id": experiment_id,
                        "start_time": start_time,
                        "tags": [{"key": k, "value": v} for k, v in tags.items()],
                    },
                )["run"]
            )
            run_id = run["info"]["run_id"]
            with self.store.transaction() as conn:
                self.store.put(
                    conn,
                    "tracking",
                    attempt,
                    b["project"],
                    {"job_id": b["job_id"], "experiment_id": experiment_id, "run_id": run_id},
                )
            context = b["candidate"]["context"]
            params = {
                "backend": b["candidate"]["backend"],
                "variant": b["variant"]["ref"],
                "image": b["variant"]["image"],
                "environment_digest": context["environment_digest"],
                "accelerator_model": context["accelerator_model"],
                "allocation_mode": context["allocation_mode"],
                **job["spec"]["identity"],
                **context["resources"],
                **{"runtime." + k: v for k, v in context["runtime_versions"].items()},
            }
            metrics = [
                {"key": k, "value": v, "timestamp": timestamp, "step": 0}
                for k, v in (result.get("measurements") or {}).items()
                if v is not None
            ]
            self.post(
                "runs/log-batch",
                {
                    "run_id": run_id,
                    "metrics": metrics,
                    "params": [{"key": k, "value": str(v)} for k, v in params.items()],
                    "tags": [{"key": k, "value": v} for k, v in tags.items()],
                },
            )
            self.post(
                "runs/update",
                {
                    "run_id": run_id,
                    "status": "FINISHED" if result["outcome"] == "COMPLETED" else "FAILED",
                    "end_time": timestamp,
                },
            )
            self.store.finish(event)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, Conflict) as exc:
            self.store.finish(event, type(exc).__name__)
        return True
