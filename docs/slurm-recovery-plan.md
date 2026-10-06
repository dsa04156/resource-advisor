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
full protocol remains unpassed; three real allocations total 217 GPU reservation
seconds, including both preceding invalid controller trials.

For a repeat, persist crash exec return code/stdout/stderr before inspecting it;
an exec's return code alone cannot establish container termination. Record the
same Pod UID and exit137/restart before restoring the template. Collect the submit
counter before any post-run assertion or cleanup, and query canonical
attempt-derived tracking/artifact refs in addition to result bodies. Persist
evidence before assertions so a verifier failure cannot erase the observation.
