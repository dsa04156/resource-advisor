# Check project integrations before starting services

The two-project GPU acceptance exposed a deployment error: a new project had a
worker route and MLflow experiment, but no result-bucket mapping. Its GPU work
completed correctly while artifact delivery retried. The outbox recovered after
the mapping was repaired; it did not require another GPU execution.

`serve` and `worker` now reject incomplete enabled project integrations **before
creating the database engine or external clients**. API credentials must use token
SHA-256 keys and an actual JSON boolean for `operator`; a string such as `"false"`
is rejected. Duplicate JSON keys are rejected rather than silently replacing a
project, token identity or route option. Errors print fixed codes without supplied
credential values, endpoints, bucket names or backend options.

## Deployment preflight

Run this with the actual private configurations intended for a deployment:

```sh
resource-advisor check-config \
  --credentials /private/credentials.json \
  --artifacts-config /private/artifacts.json \
  --worker-config /private/worker.json
```

It reads local files and constructs backend adapters for argument validation.
It does not connect to the database, scheduler, MLflow, object storage or cloud
credential providers. `--database` is not used by this command. A successful
report includes only project/route counts, enabled integrations and its scope.
Failure exits with code 2. Use it before updating Secrets or restarting services.

Checks cover:

- Every API project has a bucket mapping when artifact reading is enabled.
- Every worker route's project has a bucket mapping when artifact delivery is
  enabled, and an experiment ID when MLflow delivery is enabled.
- Project/cluster route pairs are unique, backend options pass the existing
  Kubernetes/Slurm constructor checks, and project identities are valid.
- When API and worker files are supplied together, every worker project has an
  API identity; artifact enablement and per-project bucket names agree.

Multiple routes per project and workers serving only a subset of API projects
are supported. A shared bucket remains valid because object keys are project
scoped; this is not proof of direct storage-service user isolation. Disabled
optional integrations remain supported and are explicitly reported as disabled.
Checking only one service's files cannot establish cross-service consistency.

Workers expected to run model-based studies must also set
`"optimizer_required": true` in their private worker JSON. Startup and
`check-config` then import PyTorch and BoTorch before constructing any backend or
database connection. A missing/broken import exits with
`OPTIMIZER_RUNTIME_UNAVAILABLE`; non-boolean flags are rejected. This prevents an
artifact-only service image from silently serving an intended BO deployment via
random fallback. Lightweight workers can leave this option false; numerical model
failures can still use the explicitly recorded fallback after successful startup.
Import success is not a successful model fit: qualify a bounded model calculation
in the actual worker image before launching a comparative experiment.

| Error code | Correction |
|---|---|
| `ARTIFACT_PROJECT_MAPPING_MISSING` | Add the project's intended bucket to that service's artifact map. |
| `MLFLOW_PROJECT_MAPPING_MISSING` | Add its experiment ID to the enabled worker MLflow map. |
| `API_WORKER_BUCKET_MAPPING_MISMATCH` | Align each project's reader and writer bucket mapping. |
| `API_WORKER_ARTIFACT_ENABLEMENT_MISMATCH` | Align enabled reading/delivery in the checked service pair. |
| `WORKER_PROJECT_WITHOUT_API_IDENTITY` | Check the intended API credential scope and worker route. |
| `DUPLICATE_JSON_KEY` | Remove the ambiguous duplicate from the private file. |
| `INVALID_PRINCIPAL` | Use a valid project identifier and JSON boolean operator flag. |

Do not remove a project mapping merely to clear a pending delivery. Existing
outbox items still need their original project-owned destination. After startup,
verify service readiness, scoped credentials, real scheduler access and artifact
readback separately. This command does not prove namespace selectors, runtime
bundle availability, RBAC, endpoint equivalence, bucket existence, MLflow access,
hardware compatibility or completion of already pending work.

The regression suite reproduces the E6 omission, reader/writer disagreement,
missing MLflow scope, duplicate routes and credential ambiguity. CLI tests make
database/network construction fail if attempted, proving invalid configurations
stop before side effects. The corrected two-project lab configuration passes;
the preserved pre-correction API and worker mappings fail immediately.

## Actual deployment verification — 2026-10-03 KST

The source change was deployed to the existing API and worker using their same
locked runtime dependencies. Both deployments became Ready with the expected
image digest; their running `configuration.py` and `cli.py` hashes matched the
build report. In each running container, its mounted configuration passed and a
temporary copy with one project bucket removed exited 2 with
`ARTIFACT_PROJECT_MAPPING_MISSING`, before an intentionally invalid database URL
could be used. The mounted configurations were unchanged by these negative tests.

Post-rollout readback rechecked the earlier eight E6 attempts: one ledger and
MLflow run per attempt, six identical S3/API/MLflow bundles and foreign-project
artifact denial. No GPU work was resubmitted, and no outbox item or retained trial
finalizer remained. HTTPS health returned 200 after the existing lab access
supervisor reconnected to the new API Pod. Local tests passed 508 cases.
See the [sanitized deployment evidence](../evidence/configuration-checks.json).

## Optimizer runtime repair — 2026-10-03 UTC

The later policy comparison exposed a distinct packaging failure: an artifact-only
worker image passed the earlier project-mapping checks but lacked PyTorch/BoTorch.
Five requested BO steps recorded random fallback. The trial was stopped and all
48 application results retained; it is not a successful BO comparison.

With `optimizer_required: true`, the new guard rejected that actual incomplete
image with exit 2. A CI-qualified worker-only Argo rollout now uses the unchanged
lock with both artifact and optimizer dependencies. The mounted configuration
passes, and an actual bounded qLogNEI calculation on three preserved measurements
returns model-based output without submitting or persisting new work. Existing
database fingerprints and nonworker core processes were unchanged. See the
[deployment and calculation evidence](../evidence/optimizer-repair.json). A new
prospective GPU trial is still required; this is runtime repair verification.
