# Cancellation timing and completed-result retention

Cancellation is a durable request, not proof that a GPU has stopped. The worker
now captures available scheduler evidence before deletion and records separate
request, dispatch-intent, acknowledgement and confirmation times. A retry cannot
replace the first request time. Response loss does not manufacture an acknowledgement.

Two regression tests first reproduced a result-loss defect: cancellation deleted
a Job already reported complete, and normal reconciliation kept that completed
Job in CANCEL_REQUESTED instead of collecting its result. Both paths now share
the same observation/collection handling. A completed execution retains its
validated outcome; cancellation does not override it merely because the collector
has not caught up.

## Repeatable lab procedure

Use an independently qualified GPU workload with enough preparation time to
observe the cancellation boundary. This trial reused the immutable CUDA recovery
workload: initialize a real CUDA context, wait 30 seconds, then measure numerical
matmul agreement. The wait is allocated preparation time, not useful computation.
Keep Kueue admission, normal Pod termination and existing resource protections.

1. Verify the GPU node is Ready, has no pressure, and the project has no active
   jobs or studies. Record the fresh capability, runtime digest and source digest.
2. Submit through the authenticated API with one fixed Idempotency-Key. Wait for
   the workload's CUDA-initialization marker in the actual compute Pod logs.
3. POST `/api/v1/compute/jobs/{job_id}/cancel` twice. Check that the same
   `cancel_requested_at` is returned. Keep polling until backend confirmation;
   do not treat the POST response as resource release.
4. Compare the sole ledger row with scheduler evidence. If Pod deletion removed
   the termination timestamp, allocated device seconds must remain null, even
   though the observed accelerator count is one.
5. For the completed-before-cancel case, submit a second qualified workload.
   In an isolated lab with no unrelated active work, scale only the independent
   worker Deployment to zero after CUDA starts. Leave the compute Job running.
6. Wait for the Kubernetes Job Complete condition, then POST cancel twice while
   the worker is stopped. Restore its single replica immediately, including on
   test failure. Reconciliation must collect the result without deleting the Job.
7. Verify exactly one result, usage record and MLflow run; verify result artifacts
   and restored worker readiness. Retain raw evidence without site identifiers.

The API bearer token, CA certificate, kubeconfig and private workload routes are
operator inputs. Do not paste them into public examples. Runtime Jobs remain
outside GitOps. The temporary replica change is solely a lab failure test, not a
recommended production maintenance process.

## Scope of the evidence

[Sanitized actual observations](evidence/cancellation.json) accompany these two
GPU trials on 2026-10-02:

| Actual trial | Outcome | Verified evidence |
|---|---|---|
| Cancel after CUDA initialization | CANCELED | Request-to-confirmation 13.369656 seconds; one ledger, one KILLED MLflow run; Job/Pods absent; allocation duration unknown |
| Complete while worker stopped, then request cancel | SUCCEEDED | Same compute Pod retained, one result/ledger/FINISHED MLflow run; API/S3/MLflow artifact bytes equal; 32 allocated GPU-seconds including preparation |

Worker restart was temporarily delayed by a scheduler-reported disk-pressure
taint. No protection was bypassed. DiskPressure was already false at the next
filesystem check; generated image staging was then removed. The root cause of
that transient pressure is not established. The worker returned to one Ready
replica and all ten nodes were Ready without pressure at the final check.

Unit tests separately inject an accepted cancellation whose response
is lost, recreate the worker, and verify reconciliation without a second delete
or duplicate usage. That response-loss case is simulated, not a live crash test.

The completed-before-cancel trial covers completion visible **before** the
worker's status read. The scheduler can still finish between that read and a
destructive delete. Durable termination/result capture for that narrower window,
Slurm cancellation recovery, multi-worker fencing, and node-loss behavior remain
open. No control-plane interval is reported as measured GPU utilization or exact
physical release latency.
