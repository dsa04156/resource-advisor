# Static services under Argo CD

Three manually synchronized Applications manage the existing API/inventory,
worker and PostgreSQL objects in `resource-advisor-lab`. Runtime compute Jobs
continue to belong to the worker and Kueue. No Argo CD installation or Kubernetes
upgrade is performed by this directory.

The [renderer](render.py) produces an AppProject and three Applications from a
private site file. Its example intentionally contains rejected placeholders.
Replace them with the reviewed **full Git commit** and qualified image digests;
use the exact node selectors already qualified for each pool. Keep the worker at
zero until its routes, credentials and runtime have passed the existing
[configuration](../../docs/configuration-checks.md) and
[worker](../../docs/worker-recovery.md) checks. One qualified replica is supported;
this is not an HA deployment.

```sh
uv run python deploy/argocd/render.py \
  --site /secure/resource-advisor-site.json \
  --output /secure/new-resource-advisor-applications.json
kubectl apply --dry-run=server -f /secure/new-resource-advisor-applications.json
kubectl create -f /secure/new-resource-advisor-applications.json
kubectl -n argocd get applications.argoproj.io \
  resource-advisor-services resource-advisor-worker resource-advisor-postgres
```

The output must be a new file outside the checkout and receives mode 0600. It has
no credentials but can contain private image repositories and node labels. Use
`apply` instead of `create` for an intentional subsequent revision update. Always
qualify images and inspect the actual rendered/live diff before syncing.

## Bootstrap prerequisites

Provision these separately through the existing reviewed lab procedures:

- The independent namespace, PostgreSQL data/PVC and required Secrets.
- Inventory's ServiceAccount/read-only cluster RBAC in
  [inventory-rbac.yaml](../inventory-rbac.yaml); the `services` source deliberately
  excludes cluster-level permissions.
- Existing Kueue queues/quotas and qualified GPU runtime storage.
- Worker retention permissions and additional project bindings, where enabled.
- The already installed Argo CD controller and access to the public repository.

The AppProject allows only one destination namespace and the six required static
namespaced resource kinds. It excludes cluster resources, Secrets, PVC objects,
Pod/Job objects and Kueue Workloads. PostgreSQL's existing StatefulSet still
references its retained claim. The three sources are `deploy/services`,
`deploy/worker` and `deploy/postgres`, not the repository root or `examples`.

## Controlled synchronization

There is no automated sync/self-heal, namespace creation, force/replace option or
cascading Application finalizer. Lab experiments can deliberately stop a worker
without an automatic restart by GitOps. Each actual sync below explicitly disables
pruning. These are operator defaults, not protection against a cluster admin
deliberately requesting different behavior.

Create a private JSON file containing the exact reviewed revision:

```json
{
  "operation": {
    "sync": {
      "revision": "<same full commit as the Application>",
      "prune": false,
      "syncStrategy": {"apply": {}}
    }
  }
}
```

```sh
kubectl -n argocd patch application.argoproj.io resource-advisor-postgres \
  --type=merge --patch-file /secure/manual-sync.json
kubectl -n argocd patch application.argoproj.io resource-advisor-services \
  --type=merge --patch-file /secure/manual-sync.json
kubectl -n argocd patch application.argoproj.io resource-advisor-worker \
  --type=merge --patch-file /secure/manual-sync.json
```

Require `status.operationState.phase=Succeeded`, `status.sync.status=Synced` and
`status.health.status=Healthy` for each exact Application/revision. A timeout is
not a failed operation: inspect the same operation before taking another action.
Use the fully qualified `applications.argoproj.io` kind; this lab also has another
unrelated API resource called `applications`.

`FailOnSharedResource=true` prevents adopting another Argo Application's resource.
Manual adoption also requires checking for other management systems and comparing
server-dry-run specs with live specs. The release marker is a **resource metadata
annotation**; changing it does not change the Pod template.

## Subsequent releases and fault tests

Update the private site's pinned images/revision, render a new output, inspect the
diff and apply the new Application definitions. Manually sync and validate the
services and existing result history. Keep any private kubectl overlays consistent
with this same desired release. Merely running `kubectl set image` leaves GitOps
desired state behind and must not be reported as a synchronized release.

For a bounded fault experiment, retain manual sync and record the deliberate
temporary drift. Restore the approved desired state when done, then verify all
three Applications again. Do not enable self-heal during tests that intentionally
stop collectors/workers. Never move runtime attempts into these source paths or
annotate them with an Argo tracking ID.

Removing an Application without a cascading finalizer leaves its resources;
this does not remove tracking annotations or reverse the release. De-adoption
requires a separately reviewed desired-state/ownership change. This run did not
delete Applications, StatefulSets, PVCs, Secrets or compute Jobs.

Official behavior used here: [Argo CD 3.2 inline Kustomize patches](https://argo-cd.readthedocs.io/en/release-3.2/user-guide/kustomize/),
[project scopes](https://argo-cd.readthedocs.io/en/release-3.2/user-guide/projects/),
[resource tracking](https://argo-cd.readthedocs.io/en/release-3.2/user-guide/resource_tracking/)
and [shared-resource sync checks](https://argo-cd.readthedocs.io/en/release-3.2/user-guide/sync-options/#fail-the-sync-if-a-shared-resource-is-found).
See the [actual adoption report](../../docs/gitops-adoption.md) for measured scope.
