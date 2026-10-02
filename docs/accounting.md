# Allocation accounting across outcomes

Every terminal ComputeAttempt now creates one `ra_usage` record in the same SQL
transaction as its terminal state. Successful collection, backend failure,
confirmed cancellation, pre-submission failure and result-collection timeout all
reach this path. A worker crash cannot commit the state without its ledger record;
optimistic job versioning and the attempt primary key prevent duplicate charges.
Uncertain submissions remain unresolved, not falsely terminal or free.

## Evidence and units

- Requested resources remain separate from observed allocations. A request for one
  GPU is not proof that a backend allocated one GPU.
- Kubernetes uses the scheduled workload container's resource requests and
  PodScheduled/container termination timestamps. This describes that container's
  reservation, not all Pod overhead, GPU utilization or CPU work performed.
- Slurm uses `sacct` AllocTRES and offset-bearing Submit/Start/End timestamps.
  Generic and typed GRES are not added together. Live `squeue` is listed by account;
  a purged job ID must still reach `sacct`, whereas a transport failure remains an
  observation error.
- Allocation seconds use the observed count and valid interval. Missing allocation,
  missing timezone, reversed timestamps or lost termination evidence stay unknown.
  Equal second-resolution timestamps do not establish literally zero consumption.
- A confirmed cancellation before submission proves zero allocation. Cancellation
  after possible submission does not. If a deleted Kubernetes Pod's termination
  timestamps were not captured, the record explicitly retains null duration.
- Cancellation request, first dispatch intent, acknowledged response and terminal
  confirmation have separate timestamps. They describe control-plane observations,
  not GPU execution or the instant at which hardware became free. Request and
  dispatch timestamps survive retries; a lost acknowledgement stays unknown.
- Valid benchmark compute time is distinct from reservation time. Invalid results
  cannot supply measured compute time or a recommendation profile. Container
  runtime and scheduled-to-container-start preparation intervals are separately
  identified; neither is automatically GPU compute time.

`GET /api/v1/compute/usage` returns the authenticated project's raw records.
`GET /api/v1/compute/usage/summary` separates backend, device class, allocation mode,
accelerator model and backend cluster. Known reservation totals, unknown attempts,
legacy unqualified attempts and outcome counts are explicit. Missing dimensions
stay in an unknown group. No implicit exchange rate combines GPU, virtual-slot or
NPU time, and no price/energy/useful-utilization charge is invented.

## Existing databases

No schema replacement is needed: v2 evidence is stored in the existing JSON body.
To fill *missing* records after deployment:

```sh
resource-advisor --database "$RA_DATABASE_URL" backfill-usage
```

Use this application's own DB. The command is idempotent and does not modify
existing records or fabricate historical finish times. Older records whose
allocation counts were inferred from requests are retained for audit but excluded
from the new summary's verified allocation totals. They require a separate
evidence-backed correction if original scheduler records can be recovered.

## Live acceptance on 2026-10-02

The pinned PyTorch runtime was requalified on the physical RTX 5080 before creating
a fresh immutable capability/variant/workload reference. An authenticated API
submission ran the actual matrix benchmark through Kueue. Its GPU computation
completed successfully, while an explicitly injected collector outage prevented
ingestion until the one-second collection deadline expired.

The attempt became RESULT_INVALID with **2 observed allocated GPU-seconds** and no
measured compute time/profile in the service. A second authenticated request was
cancelled before submission and recorded zero allocation. Four previously missing
terminal records were backfilled; a second pass inserted zero. PostgreSQL backup
and restoration again matched all five service tables by content.

Read-only checks of previous real Slurm jobs also recovered failed and cancelled
states with actual AllocTRES: one GPU, one CPU and known memory. This exposed and
fixed `squeue --jobs <old-id>` failing after cache purge, which previously prevented
the adapter from reaching durable accounting. It does not prove the full Slurm
Compute API/model-result transport path.

[Sanitized actual observations](evidence/failure-ledger.json) distinguish the
injected collection fault from a hardware failure. Tests cover duplicate
reconciliation, resultless terminal states, unknown cancellation times, zero
pre-submit allocation, backfill, project isolation and device-unit separation.

Remaining work includes durable Pod termination capture, Slurm step utilization,
transfer/model preparation costs, energy measurements and full rejection coverage.
MLflow delivery for resultless terminal attempts and pure preflight rejection
accounting have since been implemented. This does not mark the full
usage-accounting milestone complete.

## Cancellation evidence and completion ordering

Before destructive cancellation, the worker reads scheduler status and persists
available start/allocation evidence. If status is unavailable, the durable cancel
event retries; it does not claim that execution stopped. If the scheduler reports
execution complete, the worker collects the result instead of deleting its source.
The collection deadline still applies; the probe execution deadline no longer
interrupts collection after execution completion has been observed.

The Job API exposes `cancel_requested_at`, `cancel_dispatch_started_at` and
`cancel_acknowledged_at`. The terminal ledger also records `cancel_confirmed_at`,
`cancel_dispatch_wait_seconds` and `cancel_confirmation_seconds`. Confirmation
means the backend was observed canceled (or submission never occurred). A
successful cancel command alone does not set it. If execution completed before
the cancellation observation, its actual terminal outcome remains authoritative
and cancellation confirmation is null. Historical records are not rewritten.

Pre-cancel observation does **not** atomically fence scheduler completion against
deletion. A workload can finish between the status read and delete; complete
result/termination retention across that boundary still requires durable backend
evidence capture. Disappeared Pods therefore retain unknown allocation duration.
This worker remains qualified for a single replica, not concurrent-worker fencing.

References: [Slurm sacct](https://slurm.schedmd.com/sacct.html) and
[Kubernetes Pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/).

## Kubernetes submission timestamp correction

Kubernetes observations now supply the API server's Job creation timestamp as
`scheduler_submitted_at`. Mixing a subsecond client submit response with a
whole-second PodScheduled timestamp could otherwise produce a negative interval
and an unknown queue duration. A new GPU follow-up verified the corrected path;
old records remain immutable. Whole-second queue timestamp pairs carry
`QUEUE_WHOLE_SECOND_RESOLUTION`, including a reported interval of zero. See the
[actual approval demo](approved-gpu-demo.md) for the raw before/after evidence.
