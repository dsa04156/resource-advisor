# E7 bounded terminal-result replay protocol

Freeze before requests against the live API. Use two retained real hardware
attempts owned by one project: one SUCCEEDED with validated result/artifact/run,
one CANCELED with native termination and resultless ledger/KILLED run already
confirmed. Do not use the disconnected Slurm attempt with uncertain termination.
No new compute, result record, study, provisioning or fault injection is intended.

1. Read both current DB/API jobs, original result and attempt-dependent immutable
   entities, usage and delivered outbox rows. Require one ledger/tracking per
   terminal attempt and no pending delivery for these two IDs. Preserve states,
   versions, full rows and stable record counts before requests.
2. With the owning operator, replay the exact positive result/digest twice. Both
   must return the original SUCCEEDED attempt, without altering its version.
3. Send the positive envelope with another epoch and another attempt ID. Both
   must return 409 with stale/unrelated-attempt cause. Send a changed non-metric
   field on the terminal result with its actual digest: 409 immutable cause.
4. For the confirmed canceled attempt, send an explicitly synthetic FAILED
   negative envelope, correct identities, no measurements and test error code.
   It must return 409 immutable cause; repeat with changed epoch: 409 stale.
   This envelope is a rejection probe, never a newly observed hardware result.
5. A different project's operator must receive 404 for the original result.
   The owning ordinary researcher token must receive 403 for result ingestion.
6. Independently reread DB/API, owned result artifact bytes and the original
   MLflow runs/searches. All target rows/content/digests/statuses remain unchanged;
   exactly one ledger and run per original attempt, no newly created result,
   profile, job, usage or outbox record. The canceled job stays resultless.

Save receipts before proceeding. Stop on any unexpected HTTP cause or mutation;
do not attempt to repair evidence by deleting records or submitting compute.
Keep private full captures outside Git; publish sanitized responses, counts,
digests and limits. Existing hardware costs are not newly incurred GPU cost.
This tests live API ingestion consistency, not external-service lease fencing,
new native cancellation, collector crash or global exactly-once delivery.
