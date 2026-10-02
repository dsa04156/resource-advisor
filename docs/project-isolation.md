# Two-project GPU queue acceptance

The bounded E6 Kubernetes acceptance ran on 2026-10-03 KST against the existing
HTTPS API, PostgreSQL worker, Kueue and one qualified RTX 5080. Project A and
project B used different API identities, namespaces, LocalQueues, immutable
contracts and MLflow experiments. One trusted platform worker handled both
namespace-scoped routes. The existing aggregate quota stayed at one physical
GPU, two CPUs and 2 GiB of requested host memory.

The [frozen plan and reproduction contract](project-isolation-plan.md) defines
the scope. This is a deterministic admission/access test, not a performance or
fairness comparison. All hardware operations were assistant-executed.

## Observed admission order

Each holder occupied the GPU before the next two requests were submitted. In
both rounds, the ordinary request arrived five seconds before the high-priority
request. The high-priority Workload was admitted while that older ordinary
Workload still had no admission and no Pod. No preemption was enabled.

| Round | Project / role | Queue seconds | GPU reservation seconds | Outcome |
|---|---|---:|---:|---|
| 1 | A / holder | 0 | 32 | SUCCEEDED |
| 1 | A / high priority | 25 | 32 | SUCCEEDED |
| 1 | B / older ordinary | 66 | 32 | SUCCEEDED |
| 2 | B / holder | 0 | 32 | SUCCEEDED |
| 2 | B / high priority | 25 | 33 | SUCCEEDED |
| 2 | A / older ordinary | 66 | 32 | SUCCEEDED |

Queue and reservation timestamps have whole-second resolution. The six GPU
reservation intervals did not overlap. They total **193 seconds**, including
container preparation and the fixture's deliberate 30-second hold. A separate
new-namespace qualification used **33 seconds**, for **226 measured reservation
seconds** in this acceptance. These are allocated GPU seconds, not utilization,
energy or useful forward-compute time. The existing A qualification was reused
within its unchanged scope and validity; its cost belongs to the preceding trial.

## Scope and quota checks

- Researcher-supplied project, namespace and queue overrides returned 422.
- Foreign workload submission and foreign Job read/cancel returned 404. Project
  Job lists, overview and usage contained no other project's trial records.
- Repeating an idempotency key within a project returned the same attempt;
  the same key in two projects created distinct attempts.
- Two-GPU candidates failed capacity validation before scheduler submission.
  Server-side two-GPU Pod dry-runs in both namespaces independently failed the
  one-GPU ResourceQuota admission check.
- Valid one-GPU, three-CPU requests remained suspended with insufficient CPU
  quota and no Pod. Both were canceled through their own APIs. Each retained one
  CANCELED usage row and one KILLED MLflow run, without invented result metrics.
  Their unobserved allocation/queue intervals remain null in the ledger.
- Ordinary namespace ServiceAccounts could not create Jobs or read Pods. The
  trusted worker could create route Jobs but could not read Secrets. Actual
  runner Pods had no ServiceAccount token mount.

Every successful attempt had exactly one validated result, one usage row and one
FINISHED MLflow run in its project's experiment. All six result bundles matched
byte-for-byte across S3, the authenticated API and MLflow. The other project's
artifact request returned 404. Both canceled attempts had exactly one ledger and
one KILLED run. Final readback found no outstanding outbox events or retained
trial Pod finalizers, no overlapping GPU reservation intervals and an unchanged,
idle shared ClusterQueue. See the [sanitized evidence](evidence/project-isolation.json).

## Integration issues retained in the record

The new namespace initially matched the ClusterQueue selector but not the
installed Kueue controller/webhook opt-in selectors. Its qualification Job stayed
suspended without a Workload. Adding the site-specific label and annotating the
same Job triggered reconciliation; the original Job UID was preserved. The
public manifest now documents that private overlay requirement. No controller
selector, existing quota, node software or driver was changed.

The first new-project results reached terminal success while artifact delivery
retried a missing project-to-bucket mapping. The API and worker mappings were
completed, then their existing durable outbox retried delivery. No GPU Job was
resubmitted and no result or failure record was replaced. The two projects use
different object-key prefixes in the same lab result bucket. The reproduction
contract now requires all route, experiment and artifact mappings up front.

The lab HTTPS port-forward briefly lost its old target during API rollouts;
its existing supervisor reconnected. This access path is not production ingress
or a high-availability claim.

## Boundaries

This proves the tested API ownership boundary and trusted Kubernetes route
mapping. It does not establish direct MLflow/S3 user authorization, network
isolation, separate worker-process privileges, Slurm multi-user policy,
multi-node fairness or resistance to cluster administrators. The shared pool
does not promise an independently reserved GPU to each project. The complete
platform goal remains open in [the requirement audit](goal-audit.md).
