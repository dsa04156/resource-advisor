# Integrated operations: current-state audit

Date: 2026-10-29. Base: `2a528dd` (branch `feat/integrated-operations`).
Scope: read-only audit of the existing lifecycle, accounting and policy paths
before any change. Levels used below: **code** (exists in source), **test**
(covered by an existing automated test with an explicit scheduler double),
**live** (historical real-hardware evidence in `docs/evidence`). Nothing here is a
new hardware result.

## 1. What already exists (do not reimplement)

| Capability | Where | Level |
|---|---|---|
| Logical job + attempt identity, `(project, idempotency_key)` unique, request digest conflict | `store.py` `ra_jobs`, `service.py` `submit` | code, test |
| Durable outbox with lease token, bounded backoff, `SKIP LOCKED`, route-scoped claim | `store.py` `claim`/`finish`, `worker-route-ownership.md` | code, test (PG 8 claimants) |
| Submit intent committed (`SUBMITTING`) before remote call; transport/parse failure → `SUBMISSION_UNKNOWN` | `worker.py` `submit_one` | code, test |
| Reconcile by attempt name, never blind resubmit | `KubernetesBackend.reconcile` (name = attempt ID, job-id + context-signature annotations), `SlurmBackend.reconcile` (name + account + partition via squeue+sacct) | code, test |
| Multiple Slurm matches refused | `SlurmBackend.reconcile` raises `BackendError` | code |
| Version CAS on every job transition; delayed acknowledgements cannot regress newer state | `store.change_job`, `tests/test_concurrent_worker.py` | code, test |
| Terminal state + one `ra_usage` row in the same transaction; attempt PK prevents double charge | `store.change_job` → `record_usage`, `tests/test_accounting.py` | code, test, live (10-02) |
| Requested vs observed allocation kept separate; generic/typed GRES not summed; unknown stays null | `accounting.py`, `backends.slurm_allocation` | code, test |
| Cancel evidence capture before delete; completion wins cancel race | `worker.cancel_one`, `tests/test_worker.py` | code, test |
| Opt-in Pod termination retention (finalizer + durable `termination` receipt, released only after terminal commit) | `kubernetes_retention.py`, `deploy/worker/termination-retention-rbac.yaml` | code, test; **not enabled** in `deploy/live-training/*` |
| Kueue Workload admission owned by Job UID + LocalQueue; Slurm `%r` pending reason | `KubernetesBackend.admission`, `SlurmBackend.status` | code, test, live (`kueue-policy.json`, `slurm-policy.json`) |
| Project→cluster route ownership; QOS/priority mapping per route | `configuration.worker_configuration`, `backends.policy_priority` | code, test |
| DB disconnect recovery at cycle boundary; acknowledgement-loss recovery of same attempt | `database-recovery.md`, `tests/test_database_recovery.py` | code, test (PG double) |

## 2. Verified gaps

G1 **Unresolvable submission has no exit.** `submit_one` raises
`SubmissionUnknown("...operator resolution required")` when reconcile returns no
match; the outbox event backs off (≤300 s) forever. There is no operator API,
no record of *which* reconcile queries ran and what they returned, and the
generic `last_error` cannot distinguish "query succeeded, no match" from
"query failed" or "ambiguous/foreign match". A pre-call crash (intent committed,
process died before `submit`) lands here too, correctly but permanently.

G2 **Cancel of an uncertain submission deadlocks.** `request_cancel` on a
`SUBMISSION_UNKNOWN` job moves it to `CANCEL_REQUESTED`; `cancel_one` reconciles,
finds nothing, raises forever. The prior uncertain-submission fact is overwritten
by the cancel state.

G3 **Native policy rejection is conflated with response loss.** Any non-zero
`sbatch` / `kubectl create` becomes `SubmissionUnknown`. The recorded Slurm trial
(`docs/evidence/slurm-policy.json`) shows definitive rejections
(`Job violates accounting/QOS policy`, `Invalid qos specification`); such a job
then reconciles to no match and is stuck in G1. The forced-command gateway
(`examples/slurm_executor.py`) also collapses every failure, including `sbatch`
timeout, into one message, so the worker cannot classify even if it tried.

G4 **Reconcile ambiguity is not distinguished.** Multiple Slurm matches or a
Kubernetes Job with foreign annotations raise plain `BackendError`, recorded the
same way as an SSH failure.

G5 **No period / institution report.** `/usage/summary` sums all time for the
caller's project. No UTC period boundaries, no interval clipping, no explicit
count of attempts whose interval is unknown or whole-second, no multi-project
(institution) view. `Principal` has no institution concept.

G6 **No native waiting-reason classification.** Raw `scheduler_reason` and Kueue
conditions are stored, but quota pending (Kueue `QuotaReserved=False`, Slurm
`AssocGrp*`/`QOSGrp*`), capacity pending (`Resources`), priority pending and
rejection are not mapped to a stable diagnostic, and the route's expected
queue/account/QOS is not compared with the observed one.

G7 **Doc inconsistency on concurrency.** `accounting.md` says the worker is
qualified for a single replica; `worker-route-ownership.md` says duplicate
workers for one route are "supported". CAS + lease fencing have software tests,
but no live multi-replica qualification exists. Every deployment manifest uses
`replicas: 1` or `0`. Operational claim must remain single worker per route.

G8 **Termination evidence loss is a configuration gap, not a code gap.** Retention
exists but the live-training worker routes do not enable
`retain_termination_evidence`; deleted Pods there still yield null duration
(documented in `accounting.md`). Enabling it is a deployment decision with its
own RBAC; not changed in this software phase.

## 3. Not gaps (checked)

- Repeated reconcile/backfill adds no usage: attempt PK + existence check
  (`record_usage`), covered by `test_resultless_terminal_usage_is_atomic_and_idempotent`.
- Stale claim / lease expiry: token-fenced `finish`; `test_postgres_claim_and_stale_finish_are_fenced`.
- Lost submit response with native job created: `test_response_loss_reconciles_without_resubmission`.
- Transport failure ≠ absence: `test_transport_failure_is_unknown_not_absent_or_terminal`,
  `test_unknown_absence_is_not_permission_to_retry`.
- Cancellation race: `test_completed_execution_wins_cancel_race_and_preserves_result`.

## 4. Minimum changes chosen

Also already present and reused, not replaced: `operations.py`
(`job_attention`, `operations_snapshot`) console read model, the
`/jobs/{id}/view` seam, and the response-loss fault tools
`examples/hold_submit_response.py`, `examples/hold_slurm_submit_response.py`
and `examples/verify_slurm_response_recovery.py`.

1. Record a bounded **submission reconciliation history** on each reconcile
   (`MATCHED | NO_MATCH | AMBIGUOUS | QUERY_FAILED`, timestamps, consecutive
   no-match count) and expose it through a read-only `/jobs/{id}/lifecycle`.
2. Add an audited operator **acknowledgement** (`ACKNOWLEDGE_UNRESOLVED`) for
   uncertain submissions, including canceled ones (G1, G2). Coordinator
   correction (2026-10-29): a no-match never proves absence, so the
   acknowledgement does **not** make the job terminal, write usage, or stop
   reconciliation. It records who/why/evidence immutably, triggers one fresh
   reconcile, and a later native match is still adopted. Requires two
   consecutive successful `NO_MATCH` checks; project-scoped, operator with subject.
3. Classify **definitive native rejections** of the submit command only, with an
   exact-line allow-list of observed Slurm/Kubernetes messages, into
   `SubmissionRejected` → terminal `FAILED` with zero proven allocation. Timeouts,
   socket errors, SSH failures and `AlreadyExists` stay uncertain (G3). The
   gateway forwards only Slurm's own rejection lines.
4. Raise `SubmissionAmbiguous` for multiple/foreign matches (G4).
5. **Period usage report** with UTC `[start, end)`, clipping, explicit unknown and
   not-finalized attempts; optional institution scope only from trusted
   operator credentials with an explicit project allow-list (G5).
6. Pure **native wait classification** in lifecycle diagnostics (G6). Route drift
   is already rejected pre-submit by `scheduling.verify_adapter_plan`.
7. Docs: one worker replica per route remains the operational qualification
   (G7); retention stays a configuration decision (G8).

No scheduler algorithm, global quota, SlurmREST, OIDC, KAI, cloud or GPU handoff
work is part of this change.
