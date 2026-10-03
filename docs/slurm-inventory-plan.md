# Slurm inventory and console acceptance plan

Starting state: two real ARM exporters reach Prometheus over mTLS. Kubernetes
inventory and the existing console are live. The last controller TCP probe failed;
host metrics therefore cannot establish scheduler state or accelerator readiness.

1. Add a separate Slurm inventory configuration/collector, reusing only the
   shared Prometheus reader and immutable inventory store. It must never invoke
   kubectl, submit jobs, register capabilities or alter scheduler configuration.
2. Provide actual read-only scontrol/squeue adapters with explicit account,
   partition and pinned v0.0.42 JSON parsing. Validate scope, duplicate identities,
   warnings/errors, missing fields and invalid numbers. Keep source failures
   independent; no missing allocation or queue response may become zero.
3. Keep the controller route explicitly unconfigured in the live deployment
   until its connection/runtime is qualified. Publish real Prometheus telemetry
   with scheduler state, reservations and queues marked unknown. Software fixtures
   for a working scheduler do not qualify physical Slurm operation.
4. Preserve project isolation, immutable history and read-time/client-side expiry.
   The CPU/memory bars use independently observed host totals; configured Slurm
   capacity and reservation arithmetic stay separate and imply no admission.
5. Extend existing UI patterns with backend labels, truthful scheduler status and
   a separate Slurm queue-record summary. Unknown accelerator inventory must not
   say no accelerators. Keep current Kubernetes behavior and native controls.
6. Run meaningful SQLite/PostgreSQL tests and browser checks for fresh/stale,
   unavailable controller, missing telemetry, injection-safe labels and narrow
   viewport layout. Publish intermediate verified commits.
7. Deploy the optional collector and updated API assets through pinned manual
   Argo sync. Verify actual source hashes, API/overview/project denial, two saved
   snapshots with advancing source times, and live browser rendering. Preserve
   worker, quotas and existing Kubernetes inventory behavior.
8. Retain a clear gate for real controller-backed inventory/queue validation and
   full Slurm job execution. Do not call the whole Notion objective complete.
