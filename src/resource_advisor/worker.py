"""Durable worker; uncertain submission is reconciled, never blindly repeated."""

from datetime import datetime
from urllib.parse import urlparse

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
                try:
                    backend.validate(row)
                except (BackendError, ValueError):
                    # Pure local preparation has not contacted the scheduler.
                    # Do not turn a known configuration rejection into an
                    # ambiguous submission or leave it outside the usage ledger.
                    with self.store.transaction() as conn:
                        latest = self.store.job(conn, row["id"])
                        if latest["state"] == State.VALIDATED:
                            self.store.change_job(
                                conn,
                                latest,
                                State.FAILED,
                                dict(latest["body"], error="PRE_SUBMISSION_BACKEND"),
                            )
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
                    body["backend_observed_at"] = now().isoformat()
                    for key, value in [
                        ("started_at", observation.started_at),
                        ("backend_finished_at", observation.finished_at),
                        ("error", observation.error),
                        ("allocation", observation.allocation),
                        ("scheduler_submitted_at", observation.submitted_at),
                        ("execution_started_at", observation.execution_started_at),
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
                    self.service.ingest(
                        row["project"],
                        result,
                        envelope["digest"],
                        phase_profile=envelope.get("phase_profile"),
                        training_receipt=envelope.get("training_receipt"),
                    )
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
    def __init__(self, store, url, *, experiments, token=None, client=None, artifact_storage=None):
        import httpx

        self.store = store
        if not experiments or any(
            not isinstance(v, str) or not v.strip() for v in experiments.values()
        ):
            raise ValueError("explicit project-to-MLflow-experiment mapping required")
        self.experiments = dict(experiments)
        self.artifact_storage = artifact_storage
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
            with self.store.transaction() as conn:
                row = self.store.job(conn, b["job_id"])
                if row is None or row["state"] not in TERMINAL:
                    raise ValueError("tracking requires a durable terminal attempt")
                job, project, state = row["body"], row["project"], row["state"]
                attempt = job["attempt_id"]
                saved_result = self.store.get(conn, "result", attempt)
                # Invalid envelopes remain audit evidence, never performance observations.
                result = (
                    saved_result["body"] if saved_result and state != State.RESULT_INVALID else None
                )
                phases = self.store.get(conn, "phase_profile", attempt)
                training = self.store.get(conn, "training_receipt", attempt)
            experiment_id = self.experiments[project]
            start_time = int(
                datetime.fromisoformat(job.get("started_at") or job["created_at"]).timestamp()
                * 1000
            )
            timestamp = (
                int(datetime.fromisoformat(job["finished_at"]).timestamp() * 1000)
                if job.get("finished_at")
                else None
            )
            if result is not None and timestamp is None:
                raise ValueError("validated result requires recorded terminal observation time")
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
                "project": project,
                "resource_advisor.state": state,
                "resource_advisor.result_present": str(saved_result is not None).lower(),
                "resource_advisor.result_valid": str(result is not None).lower(),
                "evidence_kind": result["evidence_kind"] if result else "unavailable",
                "workload_signature": job["workload_signature"],
                "context_signature": job["context_signature"],
                "quality_passed": str(job.get("quality_passed", False)).lower(),
                "execution_mode": job["request"]["mode"],
                "resource_advisor.end_time_source": (
                    "terminal_observed_at" if timestamp is not None else "unknown_legacy"
                ),
            }
            if saved_result:
                tags["result_digest"] = signature(saved_result["body"])
            if job.get("error"):
                tags["resource_advisor.error"] = str(job["error"])[:500]
            if job["request"].get("study_ref"):
                tags["resource_advisor.study_id"] = job["request"]["study_ref"]
            if training:
                tags.update(
                    {
                        "training.isolated": "true",
                        "training.initial_checkpoint_digest": training["body"][
                            "initial_checkpoint_digest"
                        ],
                        "training.output_checkpoint_digest": training["body"][
                            "output_checkpoint_digest"
                        ],
                        "training.auto_promote": "false",
                    }
                )
            if job["request"].get("parent_run_ref"):
                tags["mlflow.parentRunId"] = job["request"]["parent_run_ref"]
            if found:
                owner = {t["key"]: t["value"] for t in found[0]["data"]["tags"]}
                if found[0]["info"]["experiment_id"] != experiment_id or any(
                    owner.get(key) != tags[key]
                    for key in ("project", "resource_advisor.attempt_id", "resource_advisor.job_id")
                ):
                    raise ValueError("external MLflow run ownership mismatch")
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
                    project,
                    {"job_id": b["job_id"], "experiment_id": experiment_id, "run_id": run_id},
                )
            context = job["candidate"]["context"]
            params = {
                "backend": job["candidate"]["backend"],
                "variant": job["variant"]["ref"],
                "image": job["variant"]["image"],
                "environment_digest": context["environment_digest"],
                "accelerator_model": context["accelerator_model"],
                "allocation_mode": context["allocation_mode"],
                **job["spec"]["identity"],
                **context["resources"],
                **{"runtime." + k: v for k, v in context["runtime_versions"].items()},
            }
            metrics = [
                {"key": k, "value": v, "timestamp": timestamp, "step": 0}
                for k, v in ((result or {}).get("measurements") or {}).items()
                if v is not None
            ]
            if phases and result:
                from .diagnostics import diagnose

                diagnosis = diagnose(phases["body"], evidence_kind=result["evidence_kind"])
                tags["diagnosis.hypothesis"] = diagnosis["hypothesis"] or diagnosis["status"]
                tags["diagnosis.policy"] = diagnosis["policy_version"]
                for key in set.intersection(
                    *(set(s["phases_seconds"]) for s in phases["body"]["samples"])
                ):
                    metrics.append(
                        {
                            "key": "phase_seconds." + key,
                            "value": sum(
                                s["phases_seconds"][key] for s in phases["body"]["samples"]
                            ),
                            "timestamp": timestamp,
                            "step": 0,
                        }
                    )
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
                    "status": (
                        "FINISHED"
                        if state == State.SUCCEEDED
                        else "KILLED"
                        if state == State.CANCELED
                        else "FAILED"
                    ),
                    **({"end_time": timestamp} if timestamp is not None else {}),
                },
            )
            self.store.finish(event)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, Conflict) as exc:
            self.store.finish(event, type(exc).__name__)
        return True

    def enqueue_artifacts(self):
        """Idempotently enable MLflow artifact delivery for existing S3 records."""
        with self.store.transaction() as conn:
            for row in self.store.list(conn, "artifact"):
                self.store.enqueue(
                    conn,
                    "mlflow-artifact-" + row["body"]["attempt_id"],
                    "mlflow_artifact",
                    {"artifact_ref": row["ref"]},
                )

    def deliver_artifact_one(self):
        if self.artifact_storage is None:
            return False
        from botocore.exceptions import BotoCoreError, ClientError

        from .artifacts import MAX_RESULT_BYTES, ArtifactError

        event = self.store.claim("mlflow_artifact")
        if not event:
            return False
        try:
            with self.store.transaction() as conn:
                row = self.store.get(conn, "artifact", event["body"]["artifact_ref"])
                artifact = row["body"]
                tracking = self.store.get(conn, "tracking", artifact["attempt_id"])
                if not tracking or tracking["project"] != row["project"]:
                    raise ArtifactError("MLflow run link not available")
                link = tracking["body"]
            response = self.client.get(
                "/api/2.0/mlflow/runs/get", params={"run_id": link["run_id"]}
            )
            response.raise_for_status()
            run = response.json()["run"]
            tags = {t["key"]: t["value"] for t in run["data"]["tags"]}
            if (
                run["info"]["experiment_id"] != self.experiments[row["project"]]
                or tags.get("project") != row["project"]
                or tags.get("resource_advisor.attempt_id") != artifact["attempt_id"]
            ):
                raise ArtifactError("external run ownership mismatch")
            uri = urlparse(run["info"]["artifact_uri"])
            parts = uri.path.lstrip("/").split("/")
            if (
                uri.scheme != "mlflow-artifacts"
                or uri.netloc
                or uri.query
                or uri.fragment
                or any(p in {"", ".", ".."} or not p.replace("-", "").isalnum() for p in parts)
            ):
                raise ArtifactError("same-server MLflow artifact proxy URI required")
            name = "resource-advisor/" + artifact["digest"][7:] + ".json"
            path = "/api/2.0/mlflow-artifacts/artifacts/" + "/".join(parts) + "/" + name
            data = self.artifact_storage.read(artifact)

            def read_back():
                with self.client.stream("GET", path) as response:
                    if response.status_code == 404:
                        return None
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_RESULT_BYTES:
                            raise ArtifactError("MLflow artifact exceeds size limit")
                    return bytes(content)

            existing = read_back()
            if existing is None:
                response = self.client.put(
                    path, content=data, headers={"Content-Type": "application/json"}
                )
                response.raise_for_status()
                existing = read_back()
            if existing != data:
                raise ArtifactError("MLflow artifact integrity mismatch; refusing overwrite")
            with self.store.transaction() as conn:
                self.store.put(
                    conn,
                    "artifact_tracking",
                    artifact["attempt_id"],
                    row["project"],
                    {"run_id": link["run_id"], "path": name, "digest": artifact["digest"]},
                )
            self.store.finish(event)
        except (
            httpx.HTTPError,
            BotoCoreError,
            ClientError,
            ArtifactError,
            Conflict,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            self.store.finish(event, type(exc).__name__)
        return True
