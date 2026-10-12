# Registered model → KServe → verified rollback

An operator explicitly deploys a quality-passed, registered model lifecycle. A
project user can read its history and call authenticated predictions. This bounded
unit serves the existing two-feature JSON linear model on one CPU replica using
KServe 0.15 RawDeployment. It does not execute arbitrary MLflow loader code.

```mermaid
flowchart LR
    R[REGISTERED lifecycle / explicit version] --> A[Operator deployment command]
    A --> I[Immutable intent and successor edge]
    I --> W[Worker: registry ownership and package readback]
    W --> C[Immutable model ConfigMap]
    C --> K[KServe custom predictor]
    K --> V[Current Deployment + identity + holdout predictions]
    V --> E[READY receipt and authenticated predictions]
    E --> B[Operator selects earlier READY deployment]
    B --> I
```

## API and ownership

`POST /api/v1/compute/model-deployments`, operator token and stable Idempotency-Key:

```json
{"service_ref":"linear","lifecycle_ref":"mlc-your-registered-ref","expected_deployment_ref":null}
```

The first command expects null. Later commands must name the current **desired**
deployment, even when it is pending. `GET /model-services/{service_ref}` returns
the desired command and history; `GET /model-deployments/{ref}` returns the command,
receipt, state and sanitized delivery error. Project ownership is checked on all
paths. Reusing a key with different contents returns 409. A stale predecessor or
concurrent competing successor returns 409. No additional training Job is submitted.

`POST /model-rollbacks` selects an earlier READY deployment from the same service:

```json
{"service_ref":"linear","target_deployment_ref":"mdp-earlier-ready","expected_deployment_ref":"mdp-current-desired"}
```

A rollback is a new durable command, not deletion of the latest history. It works
when a newer desired rollout is pending. Quality rejection, unfinished evaluation,
an unregistered model or an unverified rollback target cannot be deployed.

`POST /model-services/{service_ref}:predict`, owning project user token:

```json
{"instances":[[0.2,0.3],[0.6,0.1]]}
```

Inputs contain 1–512 finite two-feature rows. The response has `predictions` and
explicit version/package/deployment identity in `model`. The proxy only contacts
the verified, platform-generated internal endpoint and checks identity/output.
Pending desired deployment, unavailable upstream or stale predictor identity returns
503; it cannot silently report a previous version as the current desired model.

## Worker and runtime

Add optional `serving` to existing worker configuration. Namespace, runtime image,
node selection and ServiceAccount are operator settings, never client manifest fields.

```json
{"serving":{"project_namespaces":{"your-project":"your-model-namespace"},"image":"registry.example/predictor@sha256:REPLACE_WITH_64_HEX_DIGEST","node_selector":{"kubernetes.io/hostname":"your-cpu-node"},"service_account":"ra-predictor"}}
```

Use a qualified image containing this package and its existing API dependencies.
No additional Python dependencies are needed. MLflow configuration remains required.
The worker claims commands through the original training Job's project/backend route.
The worker Role grants namespace-scoped ISVC create/get/update, ConfigMap create/get,
and read-only Deployment/Service observations. It receives no RBAC creation or Secrets
read privilege. `deploy/worker` provides a tokenless predictor ServiceAccount with
no RoleBinding; provision equivalent namespace-scoped permissions in other namespaces.

`model_serving.py` owns operator commands, immutable successor edges and authenticated
reads/predictions. `serving_delivery.py` owns progression and registry verification.
`kserve_deployment.py` owns fenced Kubernetes writes and live verification.
`model_predictor.py` owns bounded fixed-loader package validation and KServe v1 HTTP.

The seven package files are read back from the configured same-server MLflow artifact
proxy. Model/version/run ownership, gate, saved checkpoint/evaluation lineage and all
hashes must match. The full small package is frozen in an immutable content-addressed
ConfigMap. The predictor compares its loader bytes to the platform's trusted loader
and parses JSON weights; it does not import the packaged code or access MLflow/S3/DB.
Non-root, read-only-root, capabilities drop and RuntimeDefault seccomp are explicit.

## Progression and recovery

Commands are PENDING until actual verification, READY after a saved receipt, or
SUPERSEDED when a newer desired command overtakes unfinished delivery. A READY
receipt describes its verification time; it is not continuous health monitoring.
Immutable predecessor edges serialize each service without a new database table.
Kubernetes resourceVersion and ordinal annotations fence older writes. A lost create
or update response is reconciled by reading the deterministic owned resource.

RawDeployment uses one stable `{isvc}-predictor` Service. Deployment's own observed
generation, replicas, runtime image and package volume must match. ISVC Ready alone
is insufficient: KServe 0.15 copies Deployment observedGeneration into ISVC status.
GET predictor identity and POST holdout predictions must match before READY is saved.

Rollback reapplies the selected model package to the **InferenceService** and verifies
the resulting Deployment and predictions. Editing or rolling back the owned Deployment
directly would be overwritten by KServe reconciliation. Rolling updates may briefly
serve both versions; this is not an atomic traffic switch or canary implementation.

Failures retain PENDING/error and earlier receipts rather than inventing success.
There is no automatic rollback or continuous drift reconciliation. A resource
recreated under a different ISVC UID requires investigation. ConfigMaps and historical
deployment records are retained; automatic package garbage collection is future work.

## Boundaries

`networking.kserve.io/visibility: cluster-local` suppresses external exposure for the
installed RawDeployment path. The native predictor has no application authentication;
API project authorization does not isolate arbitrary callers already inside the cluster.
Namespace-scoped create RBAC plus ownership checks is not a hostile-tenant sandbox.
Predictor images are digest-pinned but rely on operator qualification. JSON ConfigMaps
are appropriate for this tiny model only. KFP orchestration, console lineage, generic
binary models, GPU inference, canary experiments and production HA remain separate.

References: [KServe v0.15 custom predictor](https://github.com/kserve/kserve/blob/v0.15.0/docs/samples/v1beta1/custom/custom_model/custom.yaml),
[Raw status propagation](https://github.com/kserve/kserve/blob/v0.15.0/pkg/apis/serving/v1beta1/inference_service_status.go),
[Kubernetes rollout completion](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/).
