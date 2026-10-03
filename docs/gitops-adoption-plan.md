# Static service GitOps acceptance plan

Use the existing Argo CD installation without upgrading it or Kubernetes.
Adopt only the independent project's static API/inventory, worker and PostgreSQL
objects after comparing intended manifests with the running resources. Preserve
existing images, node selection, Secrets, database/PVC and compute history.

The three Applications use reviewed full commit IDs and operator-supplied,
digest-pinned images. Site image repositories/selectors are private Application
overrides; credentials remain existing Secrets. Their AppProject allows one lab
namespace and static namespaced kinds only. No Namespace, Secret, PVC, cluster
RBAC, Kueue Workload, Pod or runtime Job is a GitOps source object. Existing
inventory cluster RBAC remains a separately reviewed bootstrap prerequisite.

Use manual sync with `prune: false`, no automatic self-heal and no cascading
Application finalizer. Lab fault tests deliberately stop workers; GitOps must
not silently restart them. `FailOnSharedResource=true` refuses another
Application's managed resources. These defaults do not constrain cluster-admin
actions or authorize changing unrelated resources.

Acceptance steps:

1. Discover actual Argo CD/Kubernetes versions, existing ownership and service
   health. Freeze original object UIDs, Pod UIDs, DB fingerprints, Secrets/PVC
   resource versions and the latest completed compute Job before adoption.
2. Validate/render the private site configuration. Compare server-dry-run results
   against live specs; any unintended workload template or database change must
   be resolved before sync. Keep rejected attempts and diagnostics.
3. Create the project/Applications, require successful repository rendering, then
   explicitly sync the three pinned sources without pruning. Verify Synced and
   Healthy, unchanged original resource and Pod UIDs, and normal API/inventory.
4. Change one **Deployment metadata annotation** used for a release marker,
   leaving its Pod template untouched. Require OutOfSync detection, then manual
   sync restoring the exact desired marker without replacing Pods.
5. Verify compute Jobs have no Argo tracking ownership and do not appear in
   Application managed resources. Check completed attempt/results/usage and
   Secrets/PVC unchanged; exclude continuously appended inventory from equality.
6. Publish generic renderer, reproduce instructions, scoped measured evidence and
   remaining provisioning/HA limits. Never commit private overrides or credentials.

This plan does not replace a live Slurm execution gate. An unavailable controller
remains unavailable even when static Kubernetes deployment automation works.
