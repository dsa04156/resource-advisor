# Interactive resource request scenario

Open **자원 요청 시나리오** in the console. Select a registered single-route GPU
workload and a scheduling profile, inspect the per-job resource request, then
submit two actual jobs. The researcher selects the workload, not a GPU node.
The single-route restriction deliberately puts both requests on the same native
execution route without changing cluster quotas or interfering with other jobs.

Each request uses ordinary automatic scheduling, queueing, worker execution,
accounting and MLflow tracking. Kueue or Slurm determines admission and execution
order. A/B identify submission requests, not separate authenticated researchers;
both belong to the current project. Queue waiting is not guaranteed: sufficiently
short jobs may complete between observations and available resources may admit
both. The screen must never invent contention, FIFO order or an admission time.

The four-column board reflects observed request/queue/run/terminal state. Click a
request to inspect its resource request, selected backend queue and node, cancel
it, or open its result. Replay and the observation slider revisit persisted state
changes. Replay is accelerated by observation sequence, not wall time. QUEUED may
mean admission waiting or container preparation; native reason codes distinguish
these cases. An unavailable API leaves a clearly labeled last observation.

`POST /api/v1/compute/queue-scenarios` accepts `workload_ref`, `profile_ref` and an
Idempotency-Key. `GET /queue-scenarios` lists the ten most recent project scenarios;
`GET /queue-scenarios/{ref}` includes both ordinary job views. The durable scenario
claim and deterministic child keys allow retry after partial submission without
creating a third job. No backend calls or quota changes are added to this service.

Workers retain up to 64 state/reason changes in each job body. Event timestamps
are observation times, not inferred resource reservation timestamps. Existing job
history cannot be reconstructed retroactively. Browser polling does not write or
manufacture events. Every compute result remains in the existing job/accounting
and experiment lifecycle.

## Queue observation repair

The lab collector was configured to observe six ClusterQueues but its ClusterRole
allowed named GET access to only one. Five observations therefore returned
unavailable although the queues existed. The named read-only resource allowlist
in `deploy/inventory-rbac.yaml` now matches those six lab queue references. Keep
this list aligned with `InventoryConfig.cluster_queue_refs` in other deployments;
do not grant unrestricted queue listing to solve a named-resource mismatch.

## Observed lab run (2026-10-06)

The browser submitted the registered Slurm Orin CNN workload twice through the
normal scheduling profile. Both jobs completed successfully and each has a
separate result and MLflow run. B's observed pending reason was
`AssocGrpCpuLimit`: the account CPU allocation limit, not an inferred GPU hardware
failure. Native accounting recorded 71 seconds from B's submission to start.
The saved event stream retained A running while B waited and B starting after A
completed. The existing cluster quotas were not changed for this demonstration.

All six configured Kueue ClusterQueues now return `ok` observations following the
named read-only RBAC repair. Focused scenario/console checks passed (11), including
partial-submission retry and durable queue-reason retention. The two scenario
checks passed again after preflight rejection handling changed. Desktop and 390px
mobile views were inspected; explicit playback and historical event selection
worked, mobile document width remained 390px, and the final browser session had
no console errors. This run validates the Slurm walkthrough; a two-job Kueue
walkthrough has not been separately executed in this increment.
