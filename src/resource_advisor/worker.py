"""Durable worker; uncertain submission is reconciled, never blindly repeated."""

from datetime import datetime

import httpx
from sqlalchemy import select

from .backends import BackendError, SubmissionUnknown
from .contracts import TERMINAL, ExecutionResult, State, now, signature
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
                    if latest["state"] == State.CANCEL_REQUESTED and state not in TERMINAL:
                        state = State.CANCEL_REQUESTED
                    if state == State.QUEUED and body.get("queued_at"):
                        wait = (now() - datetime.fromisoformat(body["queued_at"])).total_seconds()
                        if wait > body["spec"]["profiling"]["max_queue_seconds"]:
                            state, body["error"] = State.CANCEL_REQUESTED, "QUEUE_DEADLINE_EXCEEDED"
                            self.store.enqueue(
                                conn,
                                "cancel-" + body["attempt_id"],
                                "cancel",
                                {"job_id": row["id"]},
                            )
                    self.store.change_job(conn, latest, state, body)
                if state == State.COLLECTING:
                    envelope = backend.result(row)
                    result = ExecutionResult.model_validate(envelope["result"])
                    self.service.ingest(row["project"], result, envelope["digest"])
            except (BackendError, Conflict, Rejected, ValueError, KeyError):
                # A temporary collector/transport failure does not mean workload failure.
                # Visible in worker metrics/logs; successful backend without a valid result
                # remains COLLECTING, never silently SUCCEEDED.
                continue


class MLflowDelivery:
    def __init__(self, store, url, *, token=None, client=None):
        import httpx

        self.store = store
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
            # Experiment 0 is the configured server default. Separate deployment/account required.
            attempt = result["attempt_id"]
            search = self.post(
                "runs/search",
                {
                    "experiment_ids": ["0"],
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
            }
            if b.get("parent_run_ref"):
                tags["mlflow.parentRunId"] = b["parent_run_ref"]
            run = (
                found[0]
                if found
                else self.post(
                    "runs/create",
                    {
                        "experiment_id": "0",
                        "start_time": int(now().timestamp() * 1000),
                        "tags": [{"key": k, "value": v} for k, v in tags.items()],
                    },
                )["run"]
            )
            run_id = run["info"]["run_id"]
            timestamp = int(now().timestamp() * 1000)
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
                    "params": [
                        {"key": "backend", "value": b["candidate"]["backend"]},
                        {"key": "variant", "value": b["variant"]["ref"]},
                    ],
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
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            self.store.finish(event, type(exc).__name__)
        return True
