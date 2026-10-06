# Slurm console cancellation and ownership checks

The deployed console now offers **작업 취소** on active jobs. Clicking it sends a
project-authenticated cancellation request, disables duplicate clicks during the
request and displays pending termination separately from a confirmed terminal
state. Terminal jobs have no cancel control. The existing API remains responsible
for authorization, idempotency and preserving completion that races cancellation.

One real Orin allocation was submitted through the API and observed RUNNING before
the browser requested cancellation. It reached CANCELED in the API and CANCELLED
in Slurm. [Raw, sanitized evidence](evidence/slurm-cancellation-v1.json) retains
the request, dispatch, acknowledgement, confirmation and accounting boundaries.

| Check | Actual observation |
|---|---|
| Request to terminal confirmation | 4.282122 s |
| Request to dispatch | 3.171006 s |
| GPU / CPU reservation | 27 s / 27 s |
| Queue wait | 1 s, whole-second scheduler resolution |
| Duplicate cancellation after completion | Same CANCELED job, HTTP 200 |
| Durable records | One API job, one ledger row, one KILLED MLflow run |
| Performance evidence | No result, profile or result artifact created |
| Cleanup | Empty compute queue; no executor guard/model process remains |

Cancellation occurred during the native runtime verification phase. The allocation
requested one GPU, but no measured CNN output was produced. This proves cleanup
of a running Slurm allocation, not cancellation during GPU kernel execution.
The ledger uses Slurm's parent allocation interval; the raw batch/model step
records retain their separately rounded termination times. Measured compute time,
utilization and power remain unknown.

A second project token received HTTP 404 for the running job's status, cancel and
artifact-list endpoints. The owning project's job remained RUNNING until its own
browser cancellation. This verifies project API isolation for this Slurm job.

A separate Linux owner submitted one held GPU request in the same Slurm account.
The scoped executor then sent a cancellation with that exact job ID/name. Its
forced gateway added its own user filter: `scancel` returned zero, but the foreign
job remained PENDING/JobHeldUser. Therefore an exit code alone does not prove a
job was cancelled. The original owner subsequently cancelled the held request;
it never received an allocation and consumed zero GPU reservation seconds.

This bounded check establishes the deployed gateway's Linux-owner filtering and
project API denial. It does **not** establish two-project Slurm account/QOS
priority fairness, arbitrary SSH isolation or multi-user device contention.
Those remain separate acceptance gates. The already verified
[successful model/result path](slurm-api-results.md) is unchanged.

Local validation was limited to JavaScript syntax and six console tests. The
browser check verified RUNNING → cancellation request → terminal display, removal
of the terminal cancel button, and the actual scheduler/database/MLflow records
above. No existing driver, QOS, account quota or scheduler configuration changed.
