# E7: actual MLflow server interruption and delivery recovery

Freeze before registration, compute or interruption. This is one bounded trial
against the existing dedicated lab MLflow3.16.1 Deployment, not a fabricated
client refusal. Require one Ready replica, unchanged image/template/PVC, zero
RUNNING MLflow runs, no active Kubernetes compute Jobs and no pending tracking
deliveries. Preserve the unrelated original disconnected Slurm attempt.

Use one current, versioned lab capability/workload binding for the already
qualified FP32 256x256 CUDA matmul fixture (seed0,20 measured iterations), based
on current Ready/resource observations and the retained CUDA positive control.
Old capability/workload/profile records remain immutable and expired records
remain expired. No driver/runtime/queue/quota changes or weakening of TTL checks.

Bound: exactly one fresh API compute Job, one idempotency key, no replacement
compute. One physical GPU, one CPU/2GiB, original Kueue route. The existing trusted
worker must submit, collect, validate and account normally during the outage.

1. Save deployment/Service/PVC and source API/worker/inventory specifications,
   original MLflow run identities/statuses and application records.
2. Scale only the lab MLflow Deployment1→0. Confirm zero serving Pods/endpoints
   and unavailable service through the actual worker's delivery error.
3. Submit the single observe Job. Require native GPU completion, valid hardware
   result and exactly one SQL usage row despite MLflow being unavailable.
   Its original MLflow outbox must remain PENDING with an actual transport/service
   error; no tracking link or invented run/performance may appear.
4. Restore the original replica count in a finally block, also on any failure.
   Bound the outage to90 seconds; observation expiration authorizes restoration,
   never replacement compute. Require the same Deployment UID/template/PVC and
   a Ready server. GPU computation is not replayed to repair experiment delivery.
5. Let the existing worker recover the SAME outbox. Independently require one
   FINISHED MLflow run, matching attempt/project/signatures/timestamps/metrics,
   byte-identical SQL/API/S3/MLflow result bundle, one native Job and one ledger.
   Repeating the original API key must return the same Job without more compute.
6. Compare all preexisting immutable result/profile/tracking/artifact and ledger
   records, preexisting MLflow run IDs/statuses and protected native specs/UIDs.
   Verify no trial allocation or trial tracking outbox remains unfinished.

Record all compute reservation, queue/runtime, outage/recovery and delivery
attempt times, including failures. Service downtime is not GPU compute time.
Never fabricate utilization, physical GPU busy time, power or energy. Keep site
addresses, credentials, raw manifests and identity-bearing reports private.

Stop if any prerequisite, digest, quality, resource or identity check fails.
Restore service and inspect the original accepted IDs. This closes only the
actual server-outage boundary; distributed exactly-once, all backend partitions,
the unresolved Slurm attempt and full platform completion remain separate.

## Preserved pre-submission tool failure

The first administrative interruption stopped before any compute submission:
the empty EndpointSlice encoded `endpoints: null`, and the verifier attempted
to iterate that value. The finally restoration completed and the original
MLflow Deployment is again1/1 Ready. SQL independently confirms zero Jobs for
the frozen project/idempotency key; the original report is retained unchanged.

Correct the predicate to treat null/absent endpoints as an empty sequence and
save the raw endpoint observation before validating it. Continue the single
unused compute slot in a separate report directory with the SAME key and SAME
immutable registered contracts. This is not replacement compute or a replay
of an accepted allocation. Include both administrative interruption records in
the report; stop on any subsequent unexpected compute outcome.
