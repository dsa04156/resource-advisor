"""Content-addressed S3 results, with DB ownership and verified read-back."""

import hashlib
import json
import re

from sqlalchemy import select

from .contracts import State, signature
from .store import Conflict, jobs

MAX_RESULT_BYTES = 2 * 1024 * 1024


class ArtifactError(ValueError):
    pass


class S3Artifacts:
    def __init__(self, *, buckets, endpoint_url=None, region_name="us-east-1", client=None):
        if not buckets or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", b) for b in buckets.values()
        ):
            raise ArtifactError("explicit project bucket mapping required")
        self.buckets = dict(buckets)
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region_name,
                config=Config(
                    connect_timeout=5,
                    read_timeout=15,
                    retries={"max_attempts": 2, "mode": "standard"},
                    s3={"addressing_style": "path"},
                ),
            )
        self.client = client

    def location(self, project, attempt, digest):
        if project not in self.buckets:
            raise ArtifactError("project has no artifact bucket")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", project) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", attempt
        ):
            raise ArtifactError("invalid artifact owner")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ArtifactError("invalid content digest")
        return self.buckets[project], f"results/{project}/{attempt}/{digest[7:]}.json"

    def read(self, record):
        bucket, key = self.location(record["project_ref"], record["attempt_id"], record["digest"])
        if (bucket, key) != (record["bucket"], record["key"]):
            raise ArtifactError("artifact location no longer matches operator configuration")
        obj = self.client.get_object(Bucket=bucket, Key=key)
        body = obj["Body"]
        try:
            data = body.read(MAX_RESULT_BYTES + 1)
        finally:
            body.close()
        if len(data) > MAX_RESULT_BYTES or len(data) != record["size_bytes"]:
            raise ArtifactError("artifact size mismatch")
        if "sha256:" + hashlib.sha256(data).hexdigest() != record["digest"]:
            raise ArtifactError("artifact digest mismatch")
        return data

    def put(self, project, attempt, job_id, payload):
        from botocore.exceptions import ClientError

        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(data) > MAX_RESULT_BYTES:
            raise ArtifactError("result bundle exceeds size limit")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        bucket, key = self.location(project, attempt, digest)
        record = {
            "ref": "artifact-" + attempt,
            "project_ref": project,
            "attempt_id": attempt,
            "job_id": job_id,
            "kind": "result_bundle",
            "digest": digest,
            "size_bytes": len(data),
            "content_type": "application/json",
            "bucket": bucket,
            "key": key,
        }
        try:
            self.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType="application/json",
                IfNoneMatch="*",
                Metadata={"sha256": digest[7:]},
            )
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            # A timed-out PUT may have succeeded. Verify existing bytes; never overwrite.
        self.read(record)
        return record


class ArtifactDelivery:
    def __init__(self, store, storage):
        self.store, self.storage = store, storage

    def enqueue_existing(self):
        """Explicit backfill when enabling storage for already validated results."""
        with self.store.transaction() as conn:
            rows = conn.execute(
                select(jobs).where(jobs.c.state.in_([State.SUCCEEDED, State.FAILED]))
            ).mappings()
            count = 0
            for row in rows:
                body = row["body"]
                if body.get("result_digest"):
                    self.store.enqueue(
                        conn,
                        "artifact-" + body["attempt_id"],
                        "artifact",
                        {"job_id": row["id"]},
                    )
                    count += 1
            return count

    def deliver_one(self):
        from botocore.exceptions import BotoCoreError, ClientError

        event = self.store.claim("artifact")
        if not event:
            return False
        try:
            with self.store.transaction() as conn:
                row = self.store.job(conn, event["body"]["job_id"])
                body = row["body"]
                result = self.store.get(conn, "result", body["attempt_id"])["body"]
                phases = self.store.get(conn, "phase_profile", body["attempt_id"])
                training = self.store.get(conn, "training_receipt", body["attempt_id"])
                if (
                    row["state"] not in {State.SUCCEEDED, State.FAILED}
                    or signature(result) != body["result_digest"]
                ):
                    raise ArtifactError("result is not validated")
            payload = {
                "schema_version": "v1",
                "result": result,
                "result_digest": body["result_digest"],
                "workload": body["spec"],
                "candidate": body["candidate"],
                "variant": body["variant"],
                "mode": body["request"]["mode"],
            }
            if phases:
                payload["phase_profile"] = phases["body"]
            if training:
                payload["training_receipt"] = training["body"]
                payload["training_isolation"] = body["training_isolation"]
            record = self.storage.put(row["project"], body["attempt_id"], row["id"], payload)
            with self.store.transaction() as conn:
                self.store.put(conn, "artifact", record["ref"], row["project"], record)
                self.store.enqueue(
                    conn,
                    "mlflow-artifact-" + body["attempt_id"],
                    "mlflow_artifact",
                    {"artifact_ref": record["ref"]},
                )
            self.store.finish(event)
        except (BotoCoreError, ClientError, ArtifactError, Conflict, KeyError, TypeError) as exc:
            self.store.finish(event, type(exc).__name__)
        return True
