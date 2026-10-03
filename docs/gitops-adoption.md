# Live adoption of static services into Argo CD

The existing **Argo CD 3.2.3** installation now manages nine static Resource Advisor
objects through three revision-pinned Applications. Their manual syncs completed
successfully and all three report **Synced / Healthy**. Kubernetes remains
**1.31.14**. No driver, image, runtime library or database version was upgraded.

The [plan](gitops-adoption-plan.md) was committed before adoption. The
[renderer and runbook](../deploy/argocd/README.md) reproduce the declarations and
manual sync workflow. [Sanitized evidence](evidence/gitops-adoption.json) records
revision, object counts, operation times, database content hashes and the checks.
Site overrides, internal identities and credentials remain private.

| Application | Managed objects |
| --- | --- |
| resource-advisor-services | API Deployment, inventory Deployment, API Service |
| resource-advisor-worker | Worker Deployment, ServiceAccount, Role, RoleBinding |
| resource-advisor-postgres | Existing PostgreSQL StatefulSet and Service |

Inventory cluster RBAC, Secrets, namespace, queues, accelerator storage and
retention/project bootstrap permissions remain outside these Applications.
Compute Jobs are not rendered or tracked by Argo CD. Terraform is not added:
no infrastructure-creation provider is selected for this deployment.

## Preflight and preserved state

The existing services had no Argo tracking owner. A server dry-run of all nine
intended objects matched every existing `spec`, role rule and binding subject.
Argo fetched the public repository at commit
`2e9d3bdaf080d544185fc5686437b678697ab584`, rendered exactly the expected objects
and reported Healthy before the manual adoption sync.

The first local preview used an absolute Kustomize base path, which the client
rejected before contacting the cluster. It was changed to a relative path; no
load-restriction bypass or replacement deployment was used. A later unqualified
`applications` read selected the unrelated `app.k8s.io` API and returned NotFound;
subsequent checks explicitly used `applications.argoproj.io`. Neither diagnostic
was treated as an Argo operation failure.

Before/after comparisons verified:

- The same nine resource UIDs and execution specs, and the same four service Pod
  UIDs. Adding GitOps tracking and release metadata caused **no Pod replacement**.
- Exact content fingerprints for jobs, outbox, studies, usage and all **2,194**
  non-inventory entity records. No experiment, result or accounting record changed.
- All namespace Secret and PVC UIDs/resource versions unchanged.
- The latest completed GPU Job's metadata unchanged, with no Argo tracking ID.
- Inventory continued appending normally: six new records during the measured
  acceptance interval. These dynamic observations are excluded from equality.
- HTTPS health returned 200. Authenticated completed-job and artifact reads still
  returned the original validated result.

## Drift detection and controlled recovery

After adoption, only the API Deployment's `resource-advisor.io/gitops-release`
annotation was changed. Its Pod template and running resources were untouched.
An explicit refresh showed **OutOfSync** while the changed marker remained live,
demonstrating that automatic self-heal was disabled. A new manual, non-pruning
sync restored the exact desired marker and returned **Synced / Healthy**.

The same database, Secret/PVC and Pod identity comparisons passed after recovery.
This proves metadata drift detection/manual reconciliation on the actual
controller. It is not an image rollback, lost-node recovery, Argo controller
disaster recovery or highly available control-plane qualification.

## Checks and remaining scope

Fourteen renderer tests reject floating revisions/images, empty node selection,
embedded repository credentials, extra namespace/policy overrides and unsupported
worker replica counts. They also verify the limited project/source/resource
boundary. Actual CRD server validation, remote rendering, non-pruning operations,
live tracking and preserved object/data identities provide separate deployment
evidence.

Exact implementation CI **37080337607** passed at `2e9d3bd` on Python 3.11 and 3.13:
**531 SQLite tests passed with two PostgreSQL-only skips; 533 PostgreSQL tests
passed**. Lint, formatting and existing packaging/console checks also passed.

The source is pinned, so future service releases must update private Application
image overrides and revision before manual sync. A later public documentation
commit does not silently upgrade the live services. Ansible host provisioning,
Slurm execution/automation, HA and production ingress remain separately open.
The fresh Slurm controller SSH check remained unreachable during this increment;
successful Kubernetes GitOps is not evidence of a working Slurm backend.
