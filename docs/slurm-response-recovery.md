# Native Slurm accepted-response and worker recovery

Three actual Orin GPU attempts exercised the existing native CNN API route.
The third directly verifies accepted `sbatch` response loss, worker SIGKILL and
recovery of the **same native job**. The normal worker, account/QOS/partition,
runtime deadline and 120-second submission lease were preserved. This is bounded
recovery evidence, not a full E7 or prospective-protocol pass.

[Sanitized observations and all costs](evidence/slurm-response-recovery.json)
retain the two invalid controller trials as well as the successful compute
result after the third crash. No production runtime change was necessary.

## Observed recovery boundary

1. The exact-attempt [SSH shim](../examples/hold_slurm_submit_response.py) forwards
   genuine `sbatch --parsable`, records its successful native ID, then holds only
   that response. The database was SUBMITTING with no external ID.
2. The unique worker process was sent SIGKILL before the transport timeout.
   Kubernetes recorded exit 137 and one restart in the same Pod.
3. The lease was allowed to expire normally. The restarted worker queried the
   existing attempt instead of blindly creating another allocation. The API
   recovered the receipt's original Slurm ID and reached SUCCEEDED.
4. Independent `squeue`/`sacct` checks found exactly one matching parent Slurm job
   for each of the three attempts, distinguishing step rows from parent jobs.
5. The third attempt has one ledger row, one FINISHED MLflow run and identical
   result bytes in S3, the authenticated project API and MLflow. Replaying the
   original API idempotency key returns the original job.
6. The complete original worker template was restored, one replica is Ready,
   the fault ConfigMap is absent and the executor-owned native queue is empty.

## Retained instrumentation failures

The first controller used a literal NUL in a Python subprocess argument, so it
never requested SIGKILL. Its finally block restored the worker through a normal
rollout. The second did not retain the failed exec's return code/stderr or worker
termination status; a container-stopped event cannot substitute for crash proof.
Both GPU attempts completed and their records/costs remain intact.

A separate disposable process control subsequently verified PID selection,
exit137 and restart without requesting an accelerator. The third GPU controller
then captured genuine acceptance and crash/restart, but its final entity query
searched only bodies and exact refs. Tracking records use attempt-derived refs
and a job-ID body, so that query omitted them. Independent database/MLflow/S3/API
verification corrected the evidence selection without submitting another job.

The finally block had already removed that Pod's ephemeral submit-call counter.
Consequently, **one accepted native parent job** is verified, while the direct
number of submit-command invocations remains unknown. The stricter
[prospective protocol](slurm-recovery-plan.md) is not marked passed. A repeat
must collect the counter and all canonical result/tracking references before
assertions or cleanup. These are verifier defects; no application recovery
defect was established by these attempts.

| Attempt | GPU reservation | Compute result | Captured crash proof |
|---|---:|---|---|
| First controller trial | 74 s | SUCCEEDED | None |
| Second controller trial | 70 s | SUCCEEDED | None |
| Third controller trial | 73 s | SUCCEEDED | Exit137, same-Pod restart |
| Total including invalid trials | **217 s** | Three durable results | One verified crash boundary |

Each numerical CNN result checks the existing fixed fixture; this is not a
trained-model accuracy or speedup claim. Third forward latency p50/p95 was
1.086619/1.172413 ms over ten runs. Its 73 reservation seconds include runtime
verification/startup, while the ten measured forwards total 0.011066 s.
Reservation time must not be presented as physical GPU activity. GPU utilization,
power and temperature remain unmeasured. Node-loss recovery, cross-project Slurm
quota/priority and distributed exactly-once guarantees remain unverified.

The unchanged qualification shim/executor/ownership code has 46 focused passing
tests and a Ruff pass. Actual scheduler, database, result-byte and restoration
checks above supply the hardware evidence; unit tests do not supply it.
