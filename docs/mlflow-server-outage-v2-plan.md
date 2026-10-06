# Actual MLflow server outage, second separately frozen trial

Freeze before any server interruption or new compute submission. This is a new
bounded trial; the original [incomplete trial](mlflow-server-outage.md), both
administrative episodes and its1GPU reservation second remain unchanged.
The separate [explicit runtime-bundle qualification](kubernetes-runtime-bundles.md)
has now passed actual CUDA execution and publication, at2GPU reservation seconds.
Neither earlier record is retrospectively declared a successful outage test.

Reuse that immutable, qualified workload/variant/capability while its capability
is CURRENT. Do not clone or refresh it, change its image or source/PVC binding,
or add a worker configuration entry under the new variant ID. The complete
execution binding explicitly references the already approved runtime bundle.
Check the actual worker route/bundle and current node Ready/resource/pressure
state against this contract before proceeding. Stop before interruption if
the capability expired or any environment/template changed.

Bound: exactly ONE new API observe Job, ONE new idempotency key, one GPU, one
CPU/2GiB, FP32 256×256 matmul,seed0,20 measured iterations. No replacement
compute, new comparison arm, quota/driver/image/source changes or changes to
the unrelated original disconnected Slurm attempt. Use the corrected public
`examples/verify_mlflow_server_outage.py`; report mappings are normalized and
empty/null EndpointSlices are handled. Supply the already registered contracts
unchanged; their idempotent registrations must preserve their content hashes.

Apply the original [server-outage protocol](mlflow-server-outage-plan.md):
one original Ready MLflow3.16.1 Deployment/PVC, zero RUNNING MLflow runs, no
pending tracking and no active Kubernetes compute; scale only that dedicated
lab server1→0; confirm no serving Pods/endpoints; let the trusted worker run,
collect, validate and account the single GPU Job while delivery fails with an
actual transport error. Save the original PENDING outbox with its error/tries
BEFORE restoring service in a finally block, with an outage limit of90seconds.

After restoration require unchanged Deployment UID/specification, Service and
PVC,1/1 Ready, SAME outbox recovery, one FINISHED run, one ledger, one native
Job/Pod, byte-identical SQL/API/S3/MLflow result, matching identity/metrics/end
time and idempotent API replay without new compute. Preserve all earlier run
bodies, immutable entities/ledgers/terminal Jobs and protected native objects.
Record outage/recovery timing and this allocation separately from the earlier
failure and qualification costs. Never label reservations as utilization/energy.

Stop on an unexpected outcome; restore and reconcile the SAME accepted IDs.
An interrupted verifier does not authorize replacement compute. A bounded pass
closes this fixture's actual MLflow outage boundary, not tenant isolation,
distributed exactly-once, disconnected Slurm accounting or full HAIRP completion.
