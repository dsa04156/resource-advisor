# E7 isolated metadata restore acceptance plan

Freeze before capturing the new backup. This is a same-version recovery rehearsal
with existing real execution history, not a destructive production failure test.

## Scope and starting conditions

Verify Notion section 17.4: metadata and artifact metadata can be backed up,
restored to a new test instance, and linked back to original runs/result files.
The source API, worker and inventory remain running. Require no active compute
or unfinished outbox work at capture. No source Pod deletion, writer shutdown,
database cutover, driver/node modification or new GPU workload is involved.

Use the existing PostgreSQL 17.6 image digest on the already qualified metadata
node. Bound the extra target to one PostgreSQL Pod, 100m CPU/256Mi requested,
1 CPU/1Gi memory limits and a new 5Gi PVC. Use distinct names, credentials and
Service; refuse to adopt an existing target. The restore target has no compute
worker, inventory collector or externally published API.

## Capture and restore

1. Start a read-only repeatable-read source transaction, export its snapshot and
   calculate canonical fingerprints of all seven current platform tables in that same
   transaction. Keep it open while the same-version `pg_dump --snapshot` finishes.
   Capture failures retain partial private files and cannot produce a PASS.
2. Save the custom-format archive and its size/SHA-256, source fingerprints,
   database version and capture time in a new private directory outside Git.
   The dump contains private data and must never be published. Do not compare
   against a later live fingerprint and mistake new inventory writes for loss.
3. Create a new empty target database and restricted application owner; restore
   the archive transactionally with no imported ownership/ACL and stop on error.
   Do not redirect production credentials or any worker to this target.
4. Verify archive integrity and restored table contents against the saved
   snapshot fingerprints. Same-count corruption, missing tables/rows and wrong
   archive bytes must fail automated regression tests.

The current-schema v2 rehearsal includes `ra_entities`, `ra_jobs`, `ra_outbox`,
`ra_studies`, `ra_usage`, `ra_scheduler_labs` and `ra_scheduler_lab_agent`.
Both experiment tables must contain actual saved history, not newly synthesized
records. Their canonical contents and project-scoped API replay must survive
restoration. The earlier five-table v1 result remains separate historical evidence.

## Restored application checks

- Every restored artifact record must match its owning project/Job/attempt and
  resolve to its original content-addressed S3 object with matching size/digest.
- Use the restored database through the real project API read paths to retrieve
  all restored result bundles; foreign-project requests must be denied. No new
  result, usage, tracking or outbox row may be created by verification.
- Every restored MLflow tracking link must resolve to the original project,
  attempt, experiment and run. Check recorded artifact links against those runs.
- Record terminal Job/usage counts, table hashes, object/link counts, capture and
  restore durations, failure records and incomplete checks. Missing required
  evidence means incomplete, not a smaller claimed pass.
- Confirm source services stay healthy and keep their original database/PVC
  identity; the inventory can continue adding observations after the snapshot.

## Cleanup and limits

Close only this rehearsal's port-forward/process handles and scale only its new
PostgreSQL StatefulSet to zero after verification. Retain the private dump,
manifest, target PVC and restore evidence for inspection. Do not delete claims or
clear finalizers to force success. Any failed target remains explicitly recorded.

The object store and MLflow are read back, not restored. A new PVC on the same
metadata node does not prove node-loss recovery, off-site retention, PITR,
high availability or recovery-time objectives. The full platform goal remains
open until the other required execution and experiment gates are verified.
