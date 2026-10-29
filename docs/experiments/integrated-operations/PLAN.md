# Frozen fault / usage / policy plan

Status: frozen before any live run (2026-10-29). Changing a bound requires a new
plan revision and digest. Software doubles, CPU/VM execution, physical
accelerator trials and historical evidence are reported separately.

## Budget (whole effort)

| Bound | Value |
|---|---|
| New native jobs | ≤ 30 |
| Run limit per job | ≤ 120 s |
| Live workload window | ≤ 30 min |
| Accelerator-slot seconds | ≤ 1800 (CPU-only jobs count 0 accelerator slots) |
| Cohorts | next cohort only after the prior one is natively terminal or explicitly unresolved and contained |

`examples/integrated_operations_trial.py` refuses plans outside these bounds and
keeps one whole-effort ledger (`--budget`): native jobs, slot seconds from each
job's actual execution limit and requested accelerators, one live window starting
at the first submission. A cohort closes only when its audit, checked against
operator-collected native receipts, finds no active native execution.

## Targets

Only owned, dedicated routes: the `ra-live-training-worker` (Kubernetes CPU
LocalQueue route) and `ra-live-training-slurm-worker` (Slurm VM CPU route). The
physical Slurm cluster is unreachable and GPU nodes are occupied by other
projects, so **no GPU-hour or GPU performance claim** can come from this plan.
CPU/VM runs validate control-plane behavior only.

Never fault the API, PostgreSQL, Kueue/Slurm controllers, other projects' jobs or
other Deployments. Confirm before each fault that exactly one worker serves the
route (no concurrent route worker) and that the namespace has no active jobs
other than the trial's.

## Stage 1 — one boundary trial per backend (2 native jobs)

Injection boundary: **native accepted → `external_id` DB commit**. Use the
existing qualification shims, not an API kill (an API kill only tests durable
enqueue):

- Slurm VM: `examples/verify_slurm_response_recovery.py` with
  `examples/hold_slurm_submit_response.py` mounted only in the dedicated worker.
  It holds the accepted `sbatch` response, SIGKILLs the worker, and verifies one
  native submit and one ledger row after restart.
- Kubernetes: `examples/hold_submit_response.py` as `/fault/kubectl` in the
  dedicated worker for one pre-created attempt; restart the worker Pod while the
  response is held.

Record per trial: attempt ID, job ID, native ID (sanitized), DB rows before/after
(state, version, outbox status), native object UID/sacct record, timestamps for
intent, native creation, kill, worker ready, reconcile match, terminal commit.
Report recovery latency as separate components: lease expiry, polling interval,
native completion, collection.

Pass: exactly one native job per attempt, state/native ID/result/usage agree,
one `ra_usage` row, replay of the same idempotency key returns the same job and
attempt. Anything else is reported as found.

## Stage 2 — frozen 10-job cohort (only if Stage 1 passed and capacity allows)

10 CPU jobs split 5/5 across the two routes, fixed keys `io-c1-00..09`,
priorities from the existing route mapping (normal/high), one worker restart
mark during the cohort (`trial.py mark --label worker-restart-*`). Observe until
settled or the 30-min window ends; then `trial.py audit` recalculates counts and
reservation seconds from raw ledger rows independently of the service code.

## Usage / policy checks during the live window

- `GET /usage/report?month=2026-10`: trial attempts appear in finalized groups or
  `not_finalized`; no unknown is shown as zero; CPU-only groups are not mixed
  with any accelerator group.
- `GET /jobs/{id}/lifecycle` for waiting jobs: the native reason and its category
  (e.g. Kueue `QuotaReserved=False`, Slurm `AssocGrpCpuLimit`) as observed.
- No policy, QOS, quota or node label is changed to produce a waiting state.
  Native rejection is only reported if it occurs naturally from existing policy.

## Not in this plan

Rejection injection by changing policies, GPU/NPU/MIG trials, multi-replica
workers, PostgreSQL faults, Pod-retention enablement (needs separate RBAC
decision), SlurmREST, cloud.
