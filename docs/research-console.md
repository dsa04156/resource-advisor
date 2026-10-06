# Research operations console

HAIRP keeps native tools responsible for their own state and exposes scoped
operational actions in one web console.

| Workspace | Working scope |
|---|---|
| Operations / jobs / queues | Existing scheduling, wait reasons, cancellation, usage; failed/canceled jobs can be resubmitted as a new execution |
| Experiments | Project-authorized MLflow experiments and paginated runs, up to four-run metric/parameter comparison, artifact directory listing and saved operator notes |
| Pipelines | Actual Kubeflow runs, task states/dependencies, linked compute jobs, registered automatic-scheduling template submission and termination |
| Notebooks | Authorized profile's notebook readiness/resources, Jupyter link, controller-backed start/stop |

This does not embed a second scheduler or duplicate MLflow/Kubeflow databases.
Model registry editing, arbitrary pipeline upload, notebook creation, cluster-wide
quota editing and unrestricted live log access are not implemented by this
increment. Native interfaces remain linked for their full functionality.

## Runtime configuration

Set `RA_RESEARCH_CONFIG_JSON` from a private Secret for the API process. Omit the
setting to keep research integrations explicitly unconfigured. Example structure:

```json
{"projects":{"team-a":{
  "mlflow":{"url":"http://mlflow.example.invalid","experiment_ids":["1"],"ui_url":"https://tracking.example.invalid"},
  "kubeflow":{"url":"http://pipelines.example.invalid","namespace":"research-a","experiment_ids":["experiment-id"],"token_file":"/var/run/research/kfp-token","ui_url":"https://research.example.invalid/pipeline","templates":{}},
  "notebooks":{"url":"https://kubernetes.default.svc","namespace":"research-a","token_file":"/var/run/research/kube-token","ca_file":"/var/run/research/ca.crt","ui_url":"https://research.example.invalid"}
}}}
```

URLs, namespace and experiment allowlists are server controlled. Tokens never go
to the browser. Mount rotated service-account projections: audience
`pipelines.kubeflow.org` for KFP and the default Kubernetes API audience for
Notebook requests. Bind only the selected profile's runs get/list/create/terminate,
experiments get/list and notebooks get/list/patch. TLS verification stays enabled
for Kubernetes. Use native authentication configuration for external services.

Compile `examples/automatic_pipeline.py`; the first YAML document is the pipeline
specification and the second is the Kubernetes platform specification. A private
registered template has `name`, `pipeline_spec` (the KFP wrapper containing
`pipeline_spec` and `platform_spec`), `parameters`, and optionally `service_account`.
Its fixed parameters are `api_url`, a qualified digest-pinned `launcher_image`,
`token_secret` and `owner_lease_seconds`. UI callers supply only workload and
scheduling profile. Launcher credentials live in the existing profile Secret.

The automatic launcher uses `--scheduling-profile` instead of a GPU candidate;
the existing candidate-based launcher remains compatible. The workflow's ownership
lease cancels its compute job after launcher loss. KFP termination is a request,
not proof of immediate compute release; follow actual job state.

## Integrity and failure handling

MLflow writes/artifact reads check run experiment ownership first. KFP detail and
termination check the configured experiment allowlist. Notebook routes fix the
namespace server-side and patch with the current resourceVersion. Starting or
stopping changes the controller annotation; no notebook storage is deleted.

Pipeline submission claims a durable project/idempotency record before remote
creation. A received run ID is saved; retries return that receipt. After an
uncertain create response the API refuses automatic replay: inspect the run named
with the request reference before manual recovery. The stable compute key also
prevents duplicate GPU execution when the launcher is retried.

Unavailable integrations are shown separately from empty healthy services. Core
compute polling remains independent. Metrics from different workload signatures
or measurement boundaries must not be treated as directly comparable. Artifact
paths are listed; this increment does not proxy arbitrary artifact URIs.

## Design references

- [NVIDIA Run:ai workload manager](https://run-ai-docs.nvidia.com/self-hosted/workloads-in-nvidia-run-ai/workloads): state/wait-time oriented operational tables and explicit row actions.
- [Kubeflow central dashboard](https://www.kubeflow.org/docs/components/central-dash/overview/): separate research workspaces for notebooks and pipeline runs.
- [MLflow comparison table](https://learn.microsoft.com/en-us/azure/databricks/mlflow/runs): select executions and compare parameter/metric columns.

The implementation reuses HAIRP's existing light surfaces, blue action color,
compact tables and native dialogs. Service-connection strips provide entry points;
experiment comparisons sit above the run table, pipeline details render only real
stages and dependencies, and notebook cards pair readiness with start/stop actions.

API references: [MLflow REST](https://mlflow.org/docs/latest/api_reference/rest-api.html),
[KFP service-account authentication](https://www.kubeflow.org/docs/components/pipelines/user-guides/core-functions/connect-api/).
