# Persistent metadata and verified cutover

The lab API and inventory collector now use a PostgreSQL StatefulSet with a 5 GiB
PVC. The previous standalone database Pod used `emptyDir`; deleting that Pod
would have deleted the live experiment history. That original database and a
private dump were retained for recovery. No existing edge runtime was changed.

## Live evidence

Both instances ran the same pinned PostgreSQL **17.6** image. Kubernetes remained
**v1.31.14**. After stopping this application's writers and confirming terminal
jobs/studies, a custom-format dump was restored transactionally into a new empty
database. A distinct `ra_app` role owns its database/tables, with no superuser,
database-creation, role-creation or replication privilege. Negative CREATE
DATABASE / CREATE ROLE checks passed.

| Table | Records copied | Content comparison |
|---|---:|---|
| `ra_entities` | 655 | Identical canonical SHA-256 |
| `ra_jobs` | 52 | Identical canonical SHA-256 |
| `ra_outbox` | 201 | Identical canonical SHA-256 |
| `ra_studies` | 8 | Identical canonical SHA-256 |
| `ra_usage` | 52 | Identical canonical SHA-256 |

The **new** database Pod was gracefully deleted before cutover. Its StatefulSet
created a different Pod UID using the same PVC UID; all five table fingerprints
still matched the frozen source. All **46** artifact objects were read and
checked against restored size/digest/location metadata. This proves consistency
with existing objects, not an object-store restore.
All 52 restored MLflow links also resolved to their expected existing experiments
through the live MLflow API; no replacement runs were created.

The API and collector restarted against the restricted account. HTTP and
certificate-verified HTTPS health/overview checks passed, all 52 jobs were
visible, invalid authorization was refused, and fresh inventory covered ten
nodes. Counts/hashes apply to the frozen copy; subsequent inventory writes are
expected to change `ra_entities`. [Sanitized evidence](../evidence/persistent-postgres.json)
contains no credentials, database row bodies, node names or private endpoints.

One initial target placement started successfully but rejected port forwarding
with HTTP 404. That unused empty target/PVC was recreated on a qualified metadata
node **before copying data**. The source stayed available until the controlled
writer pause. Pod Ready alone did not establish end-to-end database access.

## Static deployment

[`deploy/postgres`](../../deploy/postgres) contains only the headless Service and
StatefulSet. No compute Jobs belong in this Kustomization or GitOps application.
The namespace must already exist. Configure a private site overlay with an
explicitly qualified metadata node/pool and storage class before applying it;
do not let a lab metadata database land on an arbitrary edge node.
The base requires `resource-advisor.io/metadata=true`; replace that selector in
the site overlay or qualify/label a pool explicitly. Without either step it
stays Pending. The live overlay replaced the selector without changing nodes.

The pinned Alpine image's PostgreSQL UID/GID is **70**, verified in the actual
container. It runs without root, privilege escalation, extra capabilities or a
service-account token. The root filesystem is read-only; data uses the PVC and
only socket/temp paths use bounded `emptyDir`. Probes avoid a liveness restart
loop during outages. Default StatefulSet PVC retention is used without requiring
a newer Kubernetes retention-policy feature.

Provision a private mode-0600 random password file outside version control, then
create the bootstrap Secret without putting its value in command arguments:

```sh
kubectl -n resource-advisor-lab create secret generic ra-postgres-admin \
  --from-file=password=/secure/path/postgres-admin-password
kubectl apply --dry-run=server -k /secure/path/postgres-site-overlay
kubectl apply -k /secure/path/postgres-site-overlay
kubectl -n resource-advisor-lab rollout status statefulset/ra-postgres --timeout=60s
```

Use the admin connection only for provisioning, not the API. For an interactive
private admin session (the `\password` command prompts without recording it in SQL):

```sql
CREATE ROLE ra_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
\password ra_app
CREATE DATABASE resource_advisor OWNER ra_app;
REVOKE ALL ON DATABASE resource_advisor FROM PUBLIC;
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
REVOKE ALL ON DATABASE template1 FROM PUBLIC;
```

In the application database, also revoke public schema creation from PUBLIC.
Supply the app URL through a protected environment/Secret, not source or CLI
arguments. `init-db` is for a fresh installation; it is not a schema migration
tool. Never initialize or restore over the existing application database.

## Backup, compare and cutover

1. Prepare an empty independent destination of the same PostgreSQL version,
   restricted app role, PVC, private credentials and tested connection. Keep the
   old service available during preparation.
2. Stop this application's API, worker and inventory writers; require terminal
   jobs/studies and wait for their database sessions to close. Other applications
   are unaffected. Never terminate unidentified database sessions to force a pass.
3. Dump the source to a mode-0600 private file with `pg_dump --format=custom`.
   Use protected libpq service/password configuration. Record the dump digest.
4. Restore **only into the empty destination**, with `pg_restore --no-owner
   --no-acl --role=ra_app --single-transaction --exit-on-error`. The restore
   connection must be authorized to assume `ra_app`; original owner privileges
   are deliberately not imported.
5. Set `RA_DATABASE_URL` to the source and `RA_RESTORED_DATABASE_URL` to the
   destination in the protected environment, then run:

   ```sh
   uv run resource-advisor verify-db-copy
   ```

   Exit 0 means identical contents in all five platform tables; exit 1 means a
   mismatch. Only counts/hashes are printed. PostgreSQL reads use read-only,
   repeatable-read transactions. Writers must stay stopped because separate
   databases do not share one transaction snapshot.
6. Recreate only the **new destination Pod**, wait for readiness and reconnect.
   Verify a changed Pod UID, unchanged PVC UID and matching fingerprints again.
   Check referenced object bytes against restored metadata.
7. Update the app's private connection settings, restart its services, verify
   authenticated historical reads and inventory writes. Retain the original DB,
   dump and pre-cutover settings.

Before cutover, failed comparison means stop and resume the original services.
After new writes begin, rollback requires another writer pause and reconciliation
of those writes. Simply returning to the old copy loses post-cutover changes.

## Remaining operational limits

The later [isolated restore rehearsal](persistent-postgres.md) uses an exported
transaction snapshot to capture and restore without stopping source writers.
It verifies a separate instance/new PVC and all restored result/MLflow links.
The quiesced `verify-db-copy` procedure above remains the cutover workflow;
`verify-db-snapshot` compares a restore with its saved backup-time fingerprint.

- The verifier loads each table into memory and checks known table contents,
  not every schema/index/extension or external service. Missing tables fail
  rather than passing as empty. Same-count JSON corruption and missing outbox
  rows have regression tests on SQLite and PostgreSQL.
- Node-local PVC storage survives Pod replacement, **not loss of its node/disk**.
  PVC deletion can destroy data under the storage class's reclaim policy. Do
  not prune/delete the claim. No HA, off-node backup, PITR or measured recovery
  objective is established here.
- The [later service deployment](service-deployment.md) moved API/inventory to
  Kubernetes with direct service-DNS access and projected inventory credentials.
  Supervised compute workers, network policy qualification, production ingress
  and complete GitOps/provisioning remain open gates.
- This is a same-version storage cutover. Schema evolution and PostgreSQL major
  upgrades need separate migration and compatibility procedures.

References: [Kubernetes StatefulSet storage/retention](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/)
and [PostgreSQL 17 restore options](https://www.postgresql.org/docs/17/app-pgrestore.html).
