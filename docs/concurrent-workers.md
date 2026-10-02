# Two-worker execution and stale-response protection

Two cooperating workers completed one real RTX 5080 attempt with one scheduler
submission, one compute Pod, one usage record and one MLflow run. This was a
bounded acceptance test; the normal deployment remains at **one worker**.

The [frozen plan](concurrent-worker-plan.md), [machine-readable evidence](evidence/concurrent-workers.json),
[worker trace wrapper](../examples/trace_worker.py) and
[live verifier](../examples/verify_concurrent_workers.py) separate software race
tests from physical execution evidence.

## Defects reproduced and fixed

A submission outbox lease can expire while its original worker is still alive.
The next worker can reconcile the accepted external Job and advance it before
the first worker's response returns. The old implementation unconditionally
changed the latest record to QUEUED, even after RUNNING or SUCCEEDED. It now
checks external identity and only attaches acknowledgements to submitting,
uncertain or cancellation-requested states; newer execution states survive.

An old scheduler status could also overwrite newer COLLECTING state. Reconciliation
now requires the database version captured before the remote observation. A
concurrent transition makes it retry a fresh observation on the next cycle.

Two PostgreSQL collectors could both read an absent result and then attempt the
same immutable insert. The loser previously raised an unhandled unique-key error.
The insert now lets the unique key arbitrate without replacing data, verifies
the winning content and project, and retains the existing job-version check.
Different content or ownership still fails. Result, profile, ledger and outbox
updates remain in the same transaction.

Deterministic tests reproduced the three state regressions before the fix. A
real two-connection PostgreSQL barrier reproduced the duplicate-result exception.
After correction, concurrent ingestion leaves one result/profile/ledger and the
expected delivery events. Eight simultaneous PostgreSQL claimers obtain exactly
one lease; an expired owner's completion cannot finish its successor's lease.
Cancellation and conflicting immutable-content/ownership cases are also covered.
These are software tests with a scheduler double, not hardware failure injection.

## Actual GPU acceptance

The existing CUDA recovery fixture initializes one allocated GPU, waits 30 seconds
and performs 20 validated matmul work units. The wait makes observation overlap
visible; it is excluded from measured kernel time and included in GPU reservation
time. Numerical agreement is fixture correctness, not model accuracy.

| Check | Measured result |
| --- | --- |
| Traced worker Pods | Two, both Ready before and after, zero restarts |
| Overlapping observations | Both recorded the same attempt RUNNING six times |
| Scheduler submission / Job / compute Pod | One each |
| Kueue | QuotaReserved, Admitted and Finished all True |
| Final state / quality / work units | SUCCEEDED / 1.0 / 20 |
| Same API idempotency key | Original job and attempt returned |
| Usage record / MLflow run | One each; MLflow FINISHED |
| S3 / HTTPS API / MLflow artifact | Identical bytes and SHA-256 |
| Application GPU reservation | 32 seconds |
| Fresh qualification GPU reservation | 31 seconds |
| Total GPU reservation | 63 seconds |

Worker B claimed/submitted the job. Worker A observed completion, delivered the
result bundle and released retained termination evidence; worker B delivered the
MLflow metadata. MLflow artifact delivery needed two claims while the tracking
link became available. All events ended DONE, without another external run.

The qualification Job initially remained suspended because its copied metadata
omitted the queue label. The same Job was corrected before any Pod existed. No
replacement qualification was submitted and no quota was expanded. A local image
build first failed because `crane` was absent from PATH; its partial build was
retained and a new directory used with the existing explicit tool path.

## Reproduce within an authorized lab

1. Check fresh capability, source/runtime digests, node readiness/pressure and
   idle lab queues. Qualify the existing fixture and register new immutable
   capability/variant/workload references. Retain all failed attempts and costs.
2. Save the normal worker manifest and private site configuration. With no active
   unrelated jobs or studies, stop its single replica. Prepare **two temporary
   Pods** from its scoped ServiceAccount, routes, storage credentials and pinned
   image. Use unique labels outside the normal Deployment selector.
3. Mount `trace_worker.py` read-only and run `python /trace/trace_worker.py worker
   --config /config/worker.json --heartbeat-path /tmp/heartbeat`. Give the Pods
   distinct `RA_TEST_WORKER_ID` values and `restartPolicy: Never`. The wrapper
   records transport boundaries without modifying leases or scheduler calls.
4. After both Pods become Ready, provide the verifier's documented private JSON
   configuration and dedicated AWS credentials through the environment:

   ```sh
   python examples/verify_concurrent_workers.py \
     --config /secure/concurrent-worker.json \
     --report /secure/new-concurrent-worker-report.json
   ```

   The report path must be new. A timeout means inspect the saved attempt and
   existing Pods; it does not authorize creating a replacement attempt.
5. Save complete worker traces and readback. Remove only the two temporary Pods
   and trace ConfigMap, then restore the normal one-replica worker. Check the
   original result remains terminal, all events are DONE and quota is unchanged.

This run deployed the corrected source image to both API and worker. In-container
hashes matched the build manifest. After the API rollout, the first HTTPS check
hit the old port-forward target and returned EOF; the existing supervisor
reconnected, and the next check returned 200 without a manual restart. Final
authenticated result/artifact reads passed. The private deployment overlays were
updated to the same digest; no cluster, driver or compute-runtime upgrade occurred.

## Verification limits

Local SQLite ran **517 passed, 2 PostgreSQL-only skips**. PostgreSQL ran **519
passed**, including independent concurrent connections. Ruff passed. GitHub
Actions run **37078858526** passed for implementation commit `89b44c1` on Python
3.11 and 3.13, including both database suites.

This physical trial did not expire a live lease or kill a worker. The adversarial
late-response ordering is proven by the software regressions. Live expired-lease
external-service fencing, optimizer multi-leader operation, network partitions,
node failure and Slurm concurrency remain distinct gates. In particular, an
external MLflow search/create sequence is not a database-enforced global unique
constraint. This evidence does not establish exactly-once effects under arbitrary
pauses or justify scaling the production worker into an HA control plane.
