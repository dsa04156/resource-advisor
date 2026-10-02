# Concurrent worker acceptance plan

This E7 extension is fixed before running new hardware trials. It tests cooperating
workers against the existing PostgreSQL database and Kubernetes queue. It does not
qualify a highly available deployment, arbitrary network partitions or Slurm.

## Roles and starting conditions

The operator prepares an isolated second worker process with the same restricted
project routes; the researcher submits through the authenticated Compute API.
Require no active unrelated lab attempts or studies, healthy service/GPU nodes,
unchanged project and ClusterQueue quotas, and a fresh qualified CUDA fixture.
Retain original manifests, all attempt identifiers, failed runs and costs.

## Software interleavings

1. Hold worker A's accepted submit response. Expire its test lease and let B
   reconcile the same scheduler Job, observe completion and commit its result.
   Returning A's response must not reopen the terminal job or rewrite its ledger.
2. Repeat with B observing RUNNING, and with B's cancellation completing before
   A returns. Delayed acknowledgement must preserve the newer durable state.
3. Hold A's status observation while B commits newer state. Applying A's stale
   observation must not regress state, erase evidence or dispatch a new action.
4. Use real concurrent PostgreSQL connections for competing outbox claims and
   result ingestion. One intent/attempt/result/profile/ledger/tracking event must
   remain, and stale event completion must not finish a newer lease.

Scheduler doubles prove these interleavings, not physical execution.

## Live acceptance

Run one bounded, numerically validated GPU workload through two concurrent worker
processes. Capture overlapping process lifetimes, calls/claims, the sole external
Job and Pod, queue admission, immutable result, usage ledger and MLflow run.
Repeat the same API idempotency key and require the original job/attempt. Verify
S3/API/MLflow artifact equality and unchanged quota. All terminal deliveries must
finish; never manufacture missing timestamps or utilization.

The second worker must be removed after verification and the normal single-worker
deployment restored. If the concurrency boundary or fresh capability cannot be
observed, record incomplete/failed evidence and inspect the same attempt; do not
silently submit replacement work or extend qualification lifetimes.

Completion requires both software regressions and actual concurrent GPU evidence.
Expired-lease external-service fencing, Slurm concurrency, disconnected nodes and
multi-leader optimizer execution remain separately scoped until directly tested.
