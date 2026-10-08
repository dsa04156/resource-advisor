# Result artifacts and recovery

Validated results enqueue an independent `artifact` event. The worker writes a
result bundle to an operator-selected S3 bucket, reads it back and verifies every
byte before publishing its immutable DB record. An unavailable artifact service
does not rewrite the completed compute job as failed. Delivery stays pending and
retries with backoff; artifact readiness is distinct from compute success.

Each bundle contains the result, result digest, workload contract, candidate,
runtime variant and execution mode. It can contain site-specific data and belongs
in private storage. The artifact bytes have their own SHA-256 digest. Keys include
project, attempt and content digest. Conditional `If-None-Match: *` writes avoid
overwriting an existing object; a retry after an uncertain PUT verifies existing
bytes. Digest/size mismatches fail closed. This mechanism is not an S3 Object Lock
or retention policy, and it cannot prevent an independently authorized administrator
from deleting objects.

## Configuration and access

Install `uv sync --locked --extra artifacts` on the worker/API host. Supply S3
credentials through the standard AWS credential provider chain. Never put them in
a WorkloadSpec, pipeline parameter, source file or public deployment example.
Provision the bucket separately; the application does not create or delete buckets.

The private storage configuration is:

```json
{
  "endpoint_url": "https://s3.example.invalid",
  "region_name": "us-east-1",
  "buckets": {"team-a": "team-a-results"}
}
```

Use this object as the `artifacts` field of the private worker configuration, and
pass the corresponding file to the API with `--artifacts-config`. Restrict server
credentials to the intended project prefixes/buckets. Application ownership checks
and a configuration map do not replace storage-server authorization. Without storage
configuration, result delivery events stay pending rather than pretending files exist.

- `GET /api/v1/compute/jobs/{job_id}/artifacts` lists available result metadata for
  the caller's project, without exposing storage addresses or keys.
- `GET /api/v1/compute/artifacts/{ref}/content` authenticates the project and verifies
  the downloaded bytes before returning JSON and a digest ETag.
- Cross-project or unknown references return 404. Missing configuration, storage
  failures and integrity failures return 503 without returning unverified content.

Current bundles are limited to 2 MiB. Large models, multipart transfers, raw per-sample
traces and training checkpoints need a separate streaming/retention contract and remain
open work. `ArtifactDelivery.enqueue_existing()` explicitly backfills already validated
results using the same stable event IDs; it does not fabricate missing measurements.

## MLflow files

Once both the S3 record and MLflow run link exist, a separate `mlflow_artifact`
event copies the verified bundle into that run's artifact directory. The adapter
checks project, experiment and attempt ownership, accepts only a same-server
`mlflow-artifacts:/...` URI, writes a content-addressed filename and reads it back.
It will not follow arbitrary URLs returned in artifact metadata. A pre-existing
different file is rejected, not replaced. MLflow's PUT API is not a conditional
write primitive; this is an application integrity check, not a WORM guarantee.

Configure `mlflow_url`, explicit `mlflow_experiments`, and `artifacts` together in
the worker. Use `MLflowDelivery.enqueue_artifacts()` when enabling this path for
existing S3 records. A changed/missing MLflow link or unsupported URI leaves the
event pending with an error. MLflow metadata and artifact delivery have separate
states, so a FINISHED run alone is not proof that its artifacts arrived.

## Actual verification — 2026-10-02

28 genuine GPU-result bundles were written to a dedicated lab S3 service on a PVC,
read back and downloaded through the authenticated API with matching hashes. They
were also uploaded to their corresponding real MLflow runs; artifact listings and
returned bytes matched. All 28 S3 and 28 MLflow artifact delivery events completed.
The [sanitized verification record](../evidence/artifacts.json) records these checks.

During an intentional interruption of the S3 connection, the API returned 503 and
the completed compute job remained SUCCEEDED. Reconnecting restored retrieval.
Unit tests additionally cover lost PUT responses, corruption, path/owner rejection,
changed bucket configuration and unsafe or wrong-owner MLflow destinations.

The lab used a previously cached, pinned S3 implementation because its upstream
image reference currently returned 401. This does not prove a fresh deployment
can pull that image. Existing KFP was independently unavailable because its MinIO
Pod could not pull that image, and its API aborted after a 503 from storage. The
existing KFP data, images, pressure protections and deployment configuration were
not modified. KFP end-to-end execution remains unverified.

Protocol references: [S3 conditional object PUT](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html)
and [MLflow artifact REST API](https://mlflow.org/docs/latest/api_reference/rest-api.html#upload-artifact).
