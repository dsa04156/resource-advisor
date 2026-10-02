# Read-only inventory and telemetry

The collector reads authorized Kubernetes/metrics APIs, project Kueue Workloads,
and explicitly mapped Prometheus series. It stores immutable observations in
this service's own database. It never registers a qualified runtime, submits a
workload, or changes existing node/exporter configuration.

## Run

Review `deploy/inventory-rbac.yaml` against the target cluster before applying it.
The lab namespace must already exist. This grants a dedicated service account
read access to nodes, all assigned Pods, node metrics and this project's Kueue
Workloads. It grants no Secret, node proxy, exec or workload mutation permission.
All-namespace Pod visibility is necessary to count reservations from other teams;
the published snapshot excludes their Pod names, labels, commands and namespaces.
The credential remains in the collector, separate from the HTTP API.

Copy `examples/inventory-config.json` to a private location and set the authorized
node pool, project, scoped kubeconfig, queue namespaces and metric bindings.
Resource classification is explicit operator configuration: confirm whether each
resource key denotes a physical device or virtual slot. Unclassified keys remain
unknown. A GPU resource name by itself cannot establish exclusive physical use.

```sh
uv run resource-advisor init-db
uv run resource-advisor collect-inventory --config /private/inventory.json --once
uv run resource-advisor collect-inventory --config /private/inventory.json --interval-seconds 30
```

Use the same `RA_DATABASE_URL` as the independent API. The authenticated endpoint
is `GET /api/v1/compute/inventory/{cluster_ref}`. It derives the project from the
bearer credential; a different project's snapshot is not returned. No user-facing
POST endpoint accepts PromQL or credentials. Missing snapshots return 404.

For deployment, use a supervised collector and rotating service-account credential.
The live verification used a short-lived lab token and port forwards, not a durable
production installation. Collection loops are serial; an interval is the delay
after collection, not a guaranteed end-to-end update period. Upstream scrape
intervals also affect freshness. Snapshots are append-only; retention/compaction
and a Slurm inventory collector are still pending.

## Meaning of values

| Field | Meaning | Must not imply |
|---|---|---|
| capacity / allocatable | Kubernetes-reported typed resource units | Physical utilization or immediate admission |
| requested | Effective requests of assigned, nonterminal Pods | Measured CPU/GPU work |
| request_headroom | max(0, allocatable − requested) | Quota, affinity, topology, or taint eligibility |
| cpu_usage_cores | Metrics API measured CPU usage | Remaining guaranteed CPU |
| memory_working_set_bytes | Metrics API working set | Reclaimable memory or MemAvailable |
| memory_available_bytes | Explicit node-exporter MemAvailable sample | Scheduler reservation headroom |
| gpu/npu utilization | An explicitly mapped exporter-reported sample | Per-job utilization or model qualification |
| queues | Active project Workloads and their admission status | A complete quota/priority explanation |

Requests include app containers, restartable init sidecars, peak sequential init
requirements and Pod overhead. Unqualified pod-level/in-place resize accounting
fails to unknown. This follows the existing cluster's
[Kubernetes 1.31.14 resource helper semantics](https://raw.githubusercontent.com/kubernetes/kubernetes/v1.31.14/pkg/api/v1/resource/helpers.go);
see also [resource management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/).
No Kubernetes upgrade is required. Ready is the Kubernetes API's reported state,
not an independent assertion that a disconnected edge device is reachable.

## Missing, unhealthy and stale

Every signal carries value, unit, source, source observation time and status.
Absent/unavailable/unhealthy/stale values are unknown, never a manufactured zero.
An unconfigured device metric is absent, also unknown. A real reported zero stays
zero. Snapshot `ok` only means configured sources succeeded; it does not mean
all devices have complete telemetry or executable models.

Read-time freshness checks invalidate values even when the collector has stopped.
An explicit `timestamp_query` must return the original sample time, typically
`timestamp(raw_metric)`, not the evaluation timestamp attached to an instant query.
For expressions combining series, supply the oldest contributing timestamp.
A health predicate must return one; source health timestamps should be included
in the conservative timestamp expression when refreshed independently.
Ambiguous/missing samples, warnings, nonfinite and out-of-range values become
unknown. Operator bounds can reject vendor sentinel values. Bindings and units
must be qualified against the deployed exporter; arbitrary vendor series names
are not automatically treated as supported metrics.

A failed Pod listing leaves capacity visible but requested/headroom unknown.
Prometheus failure or a missing configured token does not erase Kubernetes data.
Old raw snapshots are retained for history while API views null expired values.
NPU power was deliberately left unbound in the live trial: a reported zero without
validated sensor semantics is insufficient to claim physical power measurement.

## Actual lab evidence

[Sanitized observations](evidence/inventory-live.json) contain actual values and
source timestamps, with node names replaced by aliases and private endpoints,
identifiers, labels and workload identities excluded. The trial collected ten
nodes, CPU/working-set metrics, ten MemAvailable series, two DCGM GPUs, two Jetson
GPU utilization series, and Mobilint utilization/memory/temperature. It read the
project's Kueue queue and persisted/retrieved snapshots through PostgreSQL and
the authenticated HTTP API. Hailo resource registration was observed on two
nodes; Hailo utilization was not collected. One additional advertised NVIDIA
resource also lacks a bound utilization source. No NPU model or performance
qualification is established by these observations.

Unit/integration tests cover project isolation, lost Pod visibility, stale source
timestamps, collector stop, unhealthy/nonfinite/range-invalid telemetry, missing
credentials and init/sidecar accounting. Both SQLite and PostgreSQL suites passed.
Integrated dashboards, continuous operational supervision, retention, cross-project
quota views and Slurm inventory remain separate open acceptance gates.
