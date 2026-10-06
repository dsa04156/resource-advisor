# Worker ownership across Kubernetes and Slurm routes

A worker's configured `(project, backend_cluster_id)` pairs now govern submission,
cancellation, scheduler reconciliation, workflow-owner lease expiry and terminal
evidence release. Two workers with different scheduler credentials can share the
same database without consuming or modifying each other's scheduler events.
This is application routing, not a database tenant-security boundary: processes
with shared DB credentials remain trusted control-plane components.

Previously submit/cancel/release claimed the first global outbox event and only
then resolved its backend. An unsupported route could consume a lease and accrue
retries. Reconciliation could also write an observation error on another worker's
job, and lease-expiry scanning could cancel it. A Slurm-only worker alongside the
existing Kubernetes worker was therefore not safe to enable as configured.

The SQL claim now uses the persisted job's project and cluster in a correlated
`EXISTS` filter **before** ordering, limiting and leasing. Pair membership is
exact: routes `(A, cluster-1)` and `(B, cluster-2)` do not authorize `(A, cluster-2)`.
An empty route map owns no compute jobs. Missing-job events are left untouched;
they require operator investigation, not scheduler submission. Existing
PostgreSQL `SKIP LOCKED`, lease-token fencing and unknown-submission reconciliation
remain in effect. Duplicate workers for the same route are supported as before.

Artifact and MLflow delivery retain their own existing configuration and shared
outbox behavior; this change does not introduce independent delivery-tenant
authorization. No scheduler/account/queue policy or database schema is changed.

## Verification and rollout

Tests use real SQL stores with explicitly synthetic scheduler doubles. They cover
an unrelated event sorted first, same-project/different-cluster and crossed
project/cluster pairs, empty route maps, unaffected foreign job/event snapshots,
owned cancellation, expired leases, missing-job events and stale lease completion.
The PostgreSQL suite additionally exercises eight simultaneous claimants for one
scoped event and simultaneous workers for distinct routes. These are software
concurrency checks, not a claim of live Slurm isolation or end-to-end execution.

Roll out the updated code to **all compute workers sharing the outbox** before
enabling a new Slurm route. An older worker can still claim globally. Retain at
least one configured owner for every admitted project/cluster; removing a route
leaves its queued jobs, cancellations and workflow lease handling waiting for an
owner. Restore the route or intentionally drain it; do not silently reroute its
jobs. A deployment's live verification must check the worker image/source and
current route map, plus regression execution, before claiming this gate complete.
