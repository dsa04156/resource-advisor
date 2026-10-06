# Slurm accepted-submit crash acceptance

This prospective bounded E7 test uses the existing qualified native Orin CNN,
unchanged account/QOS/partition and one GPU. It exercises the deployed Slurm
worker, not a scheduler double. Require no active project jobs/studies, no
unfinished delivery events and an empty executor-owned Slurm queue before starting.

1. Save the independent worker's deployment and route configuration. Pause that
   worker, then create one idempotent observe request using the existing profile.
2. Mount the [qualification-only SSH response shim](../examples/hold_slurm_submit_response.py)
   for that exact attempt. Run the worker with a shared process namespace only
   during this test. The shim forwards real `sbatch`, records the accepted ID,
   and holds its response without modifying the batch script.
3. Verify the receipt and database `SUBMITTING` with no external ID. Kill only
   the unique worker Python PID greater than one, before its 30-second transport
   timeout. Keep the external GPU job intact. An unobserved boundary is an
   invalid attempt, not a recovery pass.
4. Observe container exit 137/restart and preserve the existing submission lease.
   Do not resubmit, reset the lease or change the native compute deadline.
5. Require exactly one real submit invocation and one matching parent Slurm job,
   a recovered validated result, one ledger row, one MLflow run, and identical
   result bytes in project API/object storage/MLflow. Replaying the original API
   idempotency key must return the original job.
6. Restore the complete original worker template/replica count and remove only
   the test ConfigMap. Verify worker readiness and empty native queue.

The application deadline remains unchanged. Allow at most 600 seconds for test
observation; an observation deadline does not mean the external job disappeared.
If the test fails, retain the exact IDs/evidence, cancel only the owned attempt
where needed and restore the normal worker. Count every allocation, including
invalid or failed attempts. Report startup/lease/recovery delay separately from
forward latency. This test does not prove node-loss recovery, arbitrary-program
execution, cross-project Slurm quota or distributed exactly-once behavior.

## Recorded execution and verifier correction

[Actual response/crash recovery](slurm-response-recovery.md) verifies the third
attempt's accepted-without-ack boundary, same-Pod exit137/restart, same native ID,
one parent job/ledger/MLflow run and identical artifacts. The direct submit-call
counter was lost during cleanup after an evidence-query failure. Therefore this
protocol was unpassed for that attempt; its three allocations total 217 GPU
reservation seconds, including both preceding invalid controller trials.

For a repeat, persist crash exec return code/stdout/stderr before inspecting it;
an exec's return code alone cannot establish container termination. Record the
same Pod UID and exit137/restart before restoring the template. Collect the submit
counter before any post-run assertion or cleanup, and query canonical
attempt-derived tracking/artifact refs in addition to result bodies. Persist
evidence before assertions so a verifier failure cannot erase the observation.

The fourth attempt using the corrected public verifier **passes every check
above**, including one retained submit invocation, same native parent, one
ledger/MLflow run and byte-identical publication. All four attempts total
290 GPU reservation seconds. Earlier invalid evidence and costs remain retained.

## Reproduce in an already configured dedicated lab

Install the existing API/worker routes, native runtime, project MLflow/bucket
mapping and TLS trust first. Prepare a private mode0600 config with the fields
listed in the [verifier](../examples/verify_slurm_response_recovery.py) docstring;
set `lab_only:true`. Provide S3 credentials through the existing process
environment. Use the same idempotency key for the entire attempt, and a new,
nonexistent private evidence directory:

```sh
rtk proxy uv run --extra artifacts python examples/verify_slurm_response_recovery.py \
  --config /private/recovery-config.json --directory /private/recovery-evidence
```

The tool refuses a busy project/outbox/native queue or a worker serving multiple
routes. It temporarily scales only the specified lab worker, mounts the scoped
response shim and restores the complete saved template. Its reports contain
private deployment/native identities; publish only a reviewed sanitized summary.
If observation times out, inspect its saved job and backend before proceeding;
never assume the job vanished or replace its idempotency key to retry compute.
