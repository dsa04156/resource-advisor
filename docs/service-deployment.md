# Supervised API and inventory

The live lab API and inventory collector now run as separate Kubernetes
Deployments. PostgreSQL, object storage and Prometheus are reached through
in-cluster Services, without host port-forward dependencies. The old local API
and collector processes were retired. Runtime compute Jobs remain outside this
static Kustomization and retain Kueue admission.

This increment deploys **API and inventory**, not the submission/optimization
worker. That worker's routing, optimizer image, leases and live compute recovery
need a separate qualification before continuous supervised execution is enabled.

## Deployment contract

[`deploy/kustomization.yaml`](../deploy/kustomization.yaml) includes the existing
read-only inventory RBAC and [`services`](../deploy/services/services.yaml).
PostgreSQL remains a separate [persistent service](persistent-postgres.md).
The base deliberately cannot run without a site-selected image, server pool and
Secrets. It does not create/adopt a namespace or provision real credentials.

| Input | Required contents |
|---|---|
| Image override | Qualified Python 3.11 amd64 image pinned by digest; locked artifacts extra and verified kubectl binary |
| Node selector | Qualified standard kubelet/server pool; base requires `resource-advisor.io/services=true` and amd64 |
| `ra-service-db` Secret | `url`: restricted app PostgreSQL connection using in-cluster DNS |
| `ra-api-config` Secret | `credentials.json`: hashed API token map; `artifacts.json`: project bucket map/in-cluster endpoint |
| `ra-artifact-client` Secret | Dedicated `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` |
| `ra-api-tls` Secret | `tls.crt` and `tls.key`, matching the client access hostname/address |
| `ra-inventory-config` Secret | `inventory.json`: explicit node/metric bindings, `kubeconfig: null`, in-cluster Prometheus URL |

Build with the existing rootless image builder; no privileged Docker socket is
needed. Verify the kubectl binary against the official version-specific checksum
before passing it to the builder. The live client/server are both **v1.31.14**.

```sh
python examples/build_launcher.py \
  --base '<qualified Python 3.11 amd64 image>@sha256:<digest>' \
  --destination '<authorized registry>/resource-advisor/services:<version>' \
  --workdir /scratch/new-services-build \
  --extra artifacts \
  --kubectl /secure/path/verified-kubectl \
  --kubectl-sha256 '<independently verified SHA-256>'
```

The builder checks binary bytes before building, installs hash-locked wheels and
adds only application source and the verified client. Site credentials/config
are mounted later, not copied into layers. Its optional optimizer extra has a
separate CPU Torch index; dependency hashes stay mandatory. The live image here
includes artifacts only and is not qualified as an optimizer worker image.

Create the dedicated Secrets from protected files, then apply a **private** site
overlay that replaces the placeholder image and node selector:

```sh
kubectl apply --dry-run=server -k /secure/path/resource-advisor-site
kubectl apply -k /secure/path/resource-advisor-site
kubectl -n resource-advisor-lab rollout status deployment/ra-api --timeout=60s
kubectl -n resource-advisor-lab rollout status deployment/ra-inventory --timeout=60s
```

For migration, initially keep the new collector at zero replicas. Verify the
new API, stop the original collector, then enable the new one. Its Recreate
strategy prevents overlap during subsequent updates. API updates can overlap
briefly against the same durable database.

Both containers run as UID/GID 10001 with a read-only root, dropped capabilities,
no privilege escalation and bounded temporary storage. The API has **no
service-account token**. Inventory uses the existing read-only ServiceAccount
and kubelet-managed projected token, with no copied expiring local kubeconfig.
Actual negative authorization checks denied Secret reads and Job creation.
Token projection was verified; elapsed-time token renewal was not separately
fault-tested in this increment.

## Readiness and termination

API readiness checks database reachability. The collector writes an atomic
heartbeat **after a snapshot is saved**. Missing, corrupt, non-finite, future or
stale timestamps fail readiness. A restarted collector removes the old heartbeat
before its first cycle. This indicates loop progress, not that every exporter is
healthy: source failures remain explicit in the saved snapshot.

SIGTERM/SIGINT request collector shutdown after the current collection/save,
interrupting the polling wait. There is no dependency-sensitive liveness probe
that turns a database outage into repeated kills. Kubernetes restarts exited
containers. A permanently stuck loop is unready but is not automatically killed;
loop deadlines and operational alerting remain to qualify.

## Lab access and actual recovery

The existing HTTPS URL/certificate were retained using an operator-owned user
systemd [port-forward service](../deploy/resource-advisor-forward.service). This
restarts/reselects the API Pod after connection loss. It is a **lab access
tunnel**, not production ingress. Its private mode-0600 environment file provides
`RA_FORWARD_KUBECONFIG`, `RA_FORWARD_ADDRESS`, and `RA_FORWARD_PORT`; no credential
is mounted in application Pods. The existing operator credential remains an
operational trust boundary and must not be distributed to application users.

```sh
systemd-analyze --user verify deploy/resource-advisor-forward.service
# Install this unit in the operator's user unit directory after preparing the env file.
systemctl --user daemon-reload
systemctl --user enable --now resource-advisor-forward.service
```

User lingering was already enabled in the verified lab. No system login policy
was changed. A production deployment needs a separately qualified authenticated
ingress/TLS route; do not treat this tunnel as HA networking.

Live [recovery evidence](evidence/service-deployment.json):

- Deleted only the API Pod; a different Pod UID became ready automatically.
- Sent handled SIGTERM to the collector; it finished its cycle, exited **0**, and
  Kubernetes restarted it with fresh ten-node inventory.
- The access tunnel restarted without manual intervention. Certificate/hostname
  verified HTTPS again returned all **52** historical jobs.
- Job, outbox, study and usage table fingerprints were unchanged. Inventory
  entities continued to append normally.
- Combined recovery was observed after **6.78 seconds** in this one trial, not a
  promised recovery SLO or a node-failure measurement.

An earlier SIGKILL attempt from inside the collector's PID namespace did not
terminate protected PID 1. Its verifier failed and that attempt is **not** claimed
as crash recovery. The successful check proves handled termination and Pod
replacement; abrupt worker crashes and in-flight compute reconciliation remain
separate open gates. Unit/regression suites passed 193 tests on SQLite and on
an isolated PostgreSQL test database.

References: [Kubernetes ServiceAccount projection](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/)
and [kubectl version-specific binary verification](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/).
