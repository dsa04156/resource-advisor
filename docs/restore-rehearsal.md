# Isolated metadata recovery rehearsal

The earlier database migration proved a controlled cutover and same-PVC restart.
This E7 rehearsal restores a fresh backup into a separately named PostgreSQL
instance with a **new PVC**, while the production API, worker and inventory keep
running. It covers Notion section 17.4's metadata restore and result reconnection
requirement. The [plan](restore-rehearsal-plan.md) was committed before capture.

## Consistent capture without pausing writers

An exported read-only repeatable-read PostgreSQL transaction supplies both the
canonical five-table fingerprints and `pg_dump --snapshot`. The exporting
transaction stays open until the dump exits. New inventory observations can
continue committing, but they are not included in either side of this frozen
comparison. Comparing the restore with a later live fingerprint would incorrectly
report those new observations as lost data.

The [PostgreSQL 17 pg_dump documentation](https://www.postgresql.org/docs/17/app-pgdump.html)
describes synchronized snapshots and online logical backups. The actual source,
dump client and target here used the same qualified PostgreSQL **17.6** image.
No Kubernetes, database, driver or runtime-library upgrade was performed.

The capture command requires a new private directory outside the checkout. It
uses the source database/role from `RA_DATABASE_URL`, the database Pod's existing
local authentication, and an argv array without a shell or password argument.
Archive and manifest files are private; the source's table bodies, credentials
and site identities are never published. A failed dump preserves its partial
archive/stderr and an INCOMPLETE status, without a success manifest. Existing
capture directories cannot be reused to overwrite evidence.

```sh
# Supply RA_DATABASE_URL through your protected environment, not this command.
python examples/backup_metadata.py \
  --namespace resource-advisor-lab --pod ra-postgres-0 \
  --directory /private/new-recovery-capture
```

The supported transport is a qualified Kubernetes PostgreSQL Pod, not arbitrary
remote shell execution. Use a matching client/database version and a role allowed
to read the source tables. The capture status is `CAPTURED_NOT_RESTORE_VERIFIED`;
a successful dump alone never passes the recovery gate.

## Restore only to a new empty target

Provision a distinct PostgreSQL Service/StatefulSet, bootstrap Secret and PVC
using the [existing qualified base](../deploy/postgres/postgres.yaml) with a private
overlay. Change **all** target names, selectors, Service references and the admin
Secret reference; keep the validated image/node/resource settings. Use
`kubectl create`, not an operation that adopts or replaces an existing database.
This trial bounded its target to 100m CPU/256Mi requests, 1 CPU/1Gi limits and a
5Gi PVC on the already qualified metadata node.

Create a dedicated non-superuser application owner, a new empty database and
restricted public-schema/database permissions. Check that the target has no
application tables before importing. Roles are provisioned independently;
source global roles, ownership and ACLs are not imported.

```sh
# Values below are target-only examples. Inspect target identity before restore.
kubectl -n resource-advisor-lab exec -i ra-restore-e7-0 -- \
  pg_restore -U ra_restore_admin --dbname=resource_advisor_restore \
  --role=ra_restore_app --single-transaction --no-owner --no-acl \
  --exit-on-error < /private/new-recovery-capture/metadata.dump
```

Keep the recovery instance quarantined from workers/collectors. A restored
outbox or queued Job must not be replayed into production during a drill. This
trial required terminal compute and a drained outbox before capture; the
restored instance ran no worker. There was no production cutover.

Set `RA_DATABASE_URL` to the **target** through protected configuration and run:

```sh
resource-advisor verify-db-snapshot \
  --manifest /private/new-recovery-capture/manifest.json \
  --archive /private/new-recovery-capture/metadata.dump
```

Exit 0 requires the archive bytes and every restored table fingerprint to match
the saved source snapshot. Exit 1 indicates mismatch; invalid/incomplete inputs
fail. Same-row-count JSON corruption, omitted tables, changed archive bytes and
source writes after snapshot export are tested. The manifest is trusted operator
evidence with integrity hashes, not a cryptographic authenticity signature.

## Verify every restored external link

`examples/verify_restore_links.py` uses the restored DB and the actual project
API read handlers **in-process through FastAPI TestClient**. It does not deploy a
new HTTP service or exercise recovery ingress. Its private config contains
`credentials_file`, `artifacts_file`, `mlflow_url` and `project_tokens` (at least
two independently scoped project tokens). Supply S3 credentials via environment
and `RA_RESTORED_DATABASE_URL` for the restored app owner.

```sh
python examples/verify_restore_links.py \
  --config /private/recovery-link-config.json \
  --report /private/new-recovery-link-report.json
```

The verifier checks all expected artifact and tracking identities, original
Job/attempt/project ownership, restored result contents, S3 size/digest,
authenticated API bytes, foreign-project rejection, MLflow experiment/run tags
and original MLflow artifact bytes. Each execution has exactly one usage row.
It refuses empty history, missing/duplicate link coverage or unfinished outbox
work. All five restored table hashes must remain unchanged after the checks.
Keep its per-attempt report private; publish only reviewed aggregate evidence.

## Measured result — 2026-10-03 KST

| Table | Restored records | Canonical contents |
|---|---:|---|
| `ra_entities` | 3,921 | Identical |
| `ra_jobs` | 319 | Identical |
| `ra_outbox` | 1,271 | Identical |
| `ra_studies` | 24 | Identical |
| `ra_usage` | 319 | Identical |

All **5,854 records** matched the saved snapshot. The private custom-format
archive was **5,045,952 bytes**. Capture took **2.275 seconds** and transactional
restore **0.833 seconds**, excluding target provisioning and verification. These
individual observed durations do not establish a recovery-time objective.

All **306 result bundles** matched S3, restored project API and MLflow bytes;
all **319 MLflow links** resolved to their original project/attempt/experiment.
Unauthenticated Job reads returned 401 and foreign-project artifact reads 404.
Readback did not alter any restored table. Thirteen resultless terminal attempts
had tracking/usage records without invented result artifacts.

No new GPU Job, MLflow run or source database write was submitted by this drill.
Existing inventory writes continued independently. The separate target uses the
same metadata node, so the evidence is not node-loss recovery. Original object
storage and MLflow were read back, **not restored**. Object-store backup, PITR,
off-site retention, HA, schema upgrades and disaster recovery remain separate
work. All hardware and recovery operations here were assistant-executed.
