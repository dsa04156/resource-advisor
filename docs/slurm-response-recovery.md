# Native Slurm accepted-response and worker recovery

Four actual Orin GPU attempts exercised the existing native CNN API route.
The fourth passes the prospective accepted `sbatch` response-loss/worker SIGKILL
protocol, including the retained direct submit-call counter. It recovers the
**same native job**. The normal worker, account/QOS/partition, runtime deadline
and 120-second submission lease were preserved. This is bounded recovery
evidence, not all E7 failure scenarios.

[Sanitized observations and all costs](evidence/slurm-response-recovery.json)
retain the invalid controller trials and all four compute results. No production
runtime change was necessary.

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
5. Each attempt has one ledger row, one FINISHED MLflow run and identical
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

The third finally block had already removed that Pod's ephemeral submit-call
counter. Its direct command count remains unknown; it is not retroactively marked
passed. The subsequent [public verifier](../examples/verify_slurm_response_recovery.py)
queries canonical entity refs and durably captures remote evidence before
assertions/cleanup. The fourth attempt retains exactly one submit invocation at
acceptance and after recovery, passes every [prospective check](slurm-recovery-plan.md),
and restores the original deployment. Exit137 was not OOMKilled; the MLflow
project tag was also checked independently. Its saved SQLAlchemy row mappings
initially serialized as strings. They were converted losslessly with every body
digest checked and the original retained; the verifier now writes structured
dictionaries, covered by a JSON round-trip regression. No application recovery
defect was established by these attempts.

| Attempt | GPU reservation | Compute result | Captured crash proof |
|---|---:|---|---|
| First controller trial | 74 s | SUCCEEDED | None |
| Second controller trial | 70 s | SUCCEEDED | None |
| Third controller trial | 73 s | SUCCEEDED | Exit137, same-Pod restart |
| Fourth, corrected verifier | 73 s | SUCCEEDED | Exit137, same-Pod restart; full protocol PASS |
| Total including invalid trials | **290 s** | Four durable results | Two verified crash boundaries |

Each numerical CNN result checks the existing fixed fixture; this is not a
trained-model accuracy or speedup claim. Third forward latency p50/p95 was
1.086619/1.172413 ms over ten runs. Its 73 reservation seconds include runtime
verification/startup, while the ten measured forwards total 0.011066 s.
Reservation time must not be presented as physical GPU activity. GPU utilization,
power and temperature remain unmeasured. Node-loss recovery, cross-project Slurm
quota/priority and distributed exactly-once guarantees remain unverified.

The corrected verifier and response shim have 12 focused passing tests and Ruff
checks. Existing executor/ownership evidence remains applicable because that code
was unchanged. Actual scheduler, database, result-byte and restoration checks
above supply the hardware evidence; unit tests do not supply it.
