# Deployment, trial and rollback runbook

Scope: the reviewed commit of `feat/integrated-operations` onto the existing lab
namespace `resource-advisor-lab`. Uses only existing tools: `examples/update_service.py`,
`deploy/argocd/render.py` + manual sync, `deploy/live-training/*.yaml`, the
existing response-loss shims and `examples/integrated_operations_trial.py`.
No schema migration: all new data lives in existing JSON bodies and the existing
immutable entity table (`kind=submission_resolution`). No new RBAC, Secret,
tunnel, quota, QOS, node label or controller change.

Placeholders in `<...>` are filled from private files; never paste addresses,
tokens or registry paths into committed evidence.

## 0. Gates before touching anything

1. Independent review of the commit has passed; record `<commit>` (full SHA).
2. Read-only preflight (compare with the coordinator snapshot
   `../.scratch/integrated-operations-20261029/preflight-*/`):

   ```sh
   kubectl -n resource-advisor-lab get deploy -o wide
   kubectl -n resource-advisor-lab get jobs.batch -o json | jq '[.items[] | select(.status.active>0)] | length'
   kubectl -n resource-advisor-lab get workloads.kueue.x-k8s.io
   kubectl -n argocd get applications.argoproj.io resource-advisor-services resource-advisor-worker resource-advisor-postgres
   ```

   Required: `ra-api` 1/1, `ra-live-training-worker` 1/1,
   `ra-live-training-slurm-worker` 1/1, `ra-worker`/`ra-slurm-worker` 0/0,
   zero active Jobs, Applications Synced/Healthy at their current revision.
3. Database backup with the existing tool, verified before any rollout:

   ```sh
   uv run python examples/backup_metadata.py --namespace resource-advisor-lab \
     --pod <postgres-pod> --directory /secure/io-backup-<date>
   # restore-check that snapshot with the existing verify-db-snapshot procedure
   ```

## 1. Build (no dependency change)

The dependency lock is unchanged by this commit, so append only the source:

```sh
git -C <checkout> rev-parse HEAD            # must equal <commit>
uv run python examples/update_service.py \
  --runtime-report /secure/qualified-services-build-report.json \
  --destination '<registry>/resource-advisor/services:io-<short-commit>' \
  --workdir /scratch/io-<short-commit>
```

Record the output digest `<services-digest>`. The Slurm route worker image is the
same application source on the OpenSSH-client layer; rebuild it with its recorded
procedure (`docs/reference/slurm-observer-transport.md`) to `<slurm-worker-digest>`.
The Slurm VM forced-command gateway (`examples/slurm_executor.py`) changed only to
forward Slurm's own rejection lines; install it on the VM with the existing
procedure and record its SHA-256 before and after.

## 2. Roll out (API first, then route workers, one at a time)

API (Argo-managed `resource-advisor-services`): update the private site file's
`revision=<commit>` and `images.api=<services-digest>`, then

```sh
uv run python deploy/argocd/render.py --site /secure/site.json --output /secure/io-apps.json
kubectl diff -f /secure/io-apps.json            # expect revision/image only
kubectl apply -f /secure/io-apps.json
kubectl -n argocd patch applications.argoproj.io resource-advisor-services \
  --type=merge --patch-file /secure/manual-sync-<commit>.json
```

Require Succeeded/Synced/Healthy and `ra-api` ready. Do not use `kubectl set image`
for Argo-managed objects.

Live-training workers (applied manifests, not Argo sources): set the pinned
digests in private copies of `deploy/live-training/worker.yaml` and
`slurm-worker.yaml`, then for each in turn:

```sh
kubectl -n resource-advisor-lab apply --dry-run=server -f /secure/live-worker.yaml
kubectl -n resource-advisor-lab apply -f /secure/live-worker.yaml
kubectl -n resource-advisor-lab rollout status deploy/ra-live-training-worker --timeout=180s
```

`strategy: Recreate` + `replicas: 1` guarantees no concurrent route worker.

Smoke (read-only): `GET /api/v1/compute/jobs/<recent-id>/lifecycle` returns
`submission_reconciliation`/`wait`; `GET /usage/report?month=2026-10` returns
`period.boundary == "[start, end) UTC"`; existing `/usage/summary` unchanged.

## 3. Trial (only within PLAN.md bounds)

```sh
export RA_TRIAL_TOKEN=<project token>   # never written to evidence
P=/secure/io-plan-c0.json; E=/secure/io-evidence/c0
uv run python examples/integrated_operations_trial.py plan    --plan $P --evidence $E
uv run python examples/integrated_operations_trial.py submit  --plan $P --evidence $E --api <api> --ca <ca>
uv run python examples/integrated_operations_trial.py mark    --plan $P --evidence $E --label worker-restart-begin
kubectl -n resource-advisor-lab rollout restart deploy/ra-live-training-worker
kubectl -n resource-advisor-lab rollout status  deploy/ra-live-training-worker --timeout=180s
uv run python examples/integrated_operations_trial.py mark    --plan $P --evidence $E --label worker-ready
uv run python examples/integrated_operations_trial.py observe --plan $P --evidence $E --api <api> --ca <ca>
uv run python examples/integrated_operations_trial.py audit   --plan $P --evidence $E --api <api> --ca <ca>
```

The accepted-response boundary (Stage 1) uses the existing verifiers instead of a
rollout restart: `examples/verify_slurm_response_recovery.py --config /secure/srr.json
--directory /secure/io-evidence/srr` for the Slurm VM route, and
`examples/hold_submit_response.py` mounted as `/fault/kubectl` in a private
overlay of the dedicated Kubernetes worker. Remove the overlay afterwards and
re-apply the normal manifest (record the temporary drift).

Stop conditions: any foreign job/Deployment change, any job left without native
terminal state or explicit unresolved status at window end, any budget breach,
any audit finding. Do not start the next cohort until resolved.

Unresolved submissions after the window: inspect `GET /jobs/{id}/lifecycle`; if
it shows two consecutive `NO_MATCH`, an operator credential with a subject may
`POST /jobs/{id}/submission-resolution {"decision":"ACKNOWLEDGE_UNRESOLVED",
"reason":"…"}`. That is audit only: the attempt stays open and is still adopted
if the native job appears. Never resubmit it.

## 4. Rollback

Code rollback is safe for data: older code ignores the new JSON keys
(`submission_reconciliation`, `submission_resolution`, `native_rejection`,
`native_rejected`, `resource_key`) and the extra entity kind.

```sh
# API: previous revision/digest in the site file, re-render, apply, manual sync.
# Workers: re-apply the previously recorded manifests (previous digests).
kubectl -n resource-advisor-lab apply -f /secure/live-worker.previous.yaml
kubectl -n resource-advisor-lab rollout status deploy/ra-live-training-worker --timeout=180s
# Gateway: reinstall the previous slurm_executor.py and check its recorded SHA-256.
```

Caveat: after rollback a job already terminal as `NATIVE_REJECTED:*` stays
terminal (correct, the scheduler refused it); acknowledgements stay as audit
records. Database restore is only for corruption, using the verified snapshot.
