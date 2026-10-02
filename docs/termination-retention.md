# Retaining Kubernetes termination evidence

Deleting a running Job can remove its Pod before the worker reads the container's
termination timestamp. Earlier cancellation trials correctly kept GPU reservation
time unknown. A terminal observation time is not the actual allocation end and
must not be substituted for it.

The opt-in `retain_termination_evidence` Kubernetes route now adds the Pod
finalizer `resource-advisor.io/termination-evidence` at creation. Cancel requests
use foreground Job deletion. Kubelet can stop the container while the API object
remains available for the worker to read its identity, scheduling time, container
start/end/exit and actual requested allocation.

The worker commits a project-owned immutable `termination` receipt with the
attempt transition. Terminal usage and a `release_termination` outbox event commit
in that same transaction. The release worker requires the terminal database state
and receipt, then rechecks namespace, Job owner UID, Pod UID and terminal phase.
Its JSON patch tests both UID and resourceVersion and removes only this project's
finalizer. Other controllers' finalizers remain intact. Failed patches retry;
an absent Pod or already-removed finalizer makes a replay successful.

The receipt retains the original timestamps after the Pod is deleted. The usage
record links it through `termination_receipt_ref`. Missing boundaries stay null;
this is reservation accounting, not useful GPU compute time, utilization or
energy attribution. Pre-existing ledger rows are not rewritten.

## Completion and failure races

A retained Pod that completed successfully during a cancellation is collected
through its Pod log target. Its result still needs normal identity, digest and
quality validation. Collection keeps the finalizer until a terminal database
outcome, including the existing collection deadline. A Job condition that becomes
terminal before its Pod stops is not sufficient to release the evidence.

Worker interruption before observation leaves the Pod retained. Interruption
after the database commit leaves an outbox event for the next worker. A failed
database transaction creates neither usage nor a release event. Patch response
loss is retried without removing any other finalizer or adding another ledger.
Observation failures and foreign object identities do not authorize cleanup.

## Enabling on a qualified route

This option defaults to false. Verify the target kubelet's termination behavior
and the deployment's worker recovery before enabling it. The tested lab uses
Kubernetes and kubelet 1.31.14 on its single allowlisted GPU node. This does not
qualify old EdgeCore/kubelet combinations or upgrade any node.

1. Deploy the updated API and worker image with the same dependency lock.
2. Apply `deploy/worker/termination-retention-rbac.yaml` in the dedicated lab
   namespace. This grants the worker Pod patch permission only in that namespace;
   ordinary runner containers still have no ServiceAccount token.
3. Set `retain_termination_evidence: true` in the administrator-owned Kubernetes
   route and restart the worker to load its configuration.
4. Verify a bounded running cancellation and completion-before-cancel race with
   a qualified workload, then check the receipt, ledger and release event.

Kubernetes documents finalizers as deletion barriers, and kubelet terminal-phase
updates before deletion since 1.27, with exceptions such as some force-deleted
Pods. See the official [Pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)
and [finalizer lifecycle](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/).
The actual installed-version lab trial is the compatibility evidence here.

## Recovery and limits

If cleanup is pending, first inspect the worker heartbeat, outbox `last_error`,
stored receipt and the exact Pod UID. Restore the same worker and permissions;
do not blanket-remove finalizers or manufacture timestamps. A UID/resourceVersion
conflict requires a new observation/retry, not an unconditional patch. Do not
downgrade the worker or disable this route until its retained Pods and cleanup
events have drained. Never remove another controller's finalizer.

An unreachable node may not publish container termination. Its resource usage
remains unknown and a retained object can require operator recovery. Force
deletion, administrative finalizer removal and loss of the metadata database can
defeat this evidence mechanism. The route still assumes one non-retrying Pod per
attempt; replacement/multiple Pods are rejected and require reconciliation.
This is not a general multi-worker fencing or node-loss recovery proof.

## Validation

`tests/test_kubernetes_retention.py` uses explicit scheduler doubles to cover
durable receipt/ledger ordering, worker restart, accepted patch response loss,
foreign/recreated/live Pods, preservation of other finalizers, completion races,
terminal Job versus live Pod ordering and failed database commit. Existing
legacy cancellation tests continue to require unknown allocation when there is
no retained termination evidence.

```sh
uv run pytest -q tests/test_kubernetes_retention.py tests/test_accounting.py \
  tests/test_backends.py tests/test_worker.py tests/test_health.py
```

## Actual lab acceptance — 2026-10-03 KST

A fresh qualified RTX 5080 execution preceded two independent API attempts:

| Scenario | Observed outcome | GPU reservation | Ledger / MLflow |
|---|---|---:|---|
| Running cancellation, worker stopped, accepted-delete response discarded by test adapter | Terminal Pod retained; worker restart committed evidence and released it | 8 seconds | One CANCELED row / one KILLED run; no fabricated result |
| Work completes while worker stopped, then duplicate cancellation requests | SUCCEEDED retained; Pod logs preserved and retention finalizer released | 32 seconds | One ledger / one FINISHED run; identical S3, API and MLflow artifact bytes |

The fresh qualification used another 32 GPU reservation seconds. Total measured
reservation was 72 seconds, including preparation and the qualification; the
fixed 30-second wait in the recovery fixture is deliberately not benchmark time.
Raw Pod UIDs, owner UIDs and container timestamps matched SQL receipts and ledger
boundaries. Both API usage responses matched SQL. At the end, API, worker and
inventory were Ready, HTTPS health returned 200, and no platform Jobs, outbox
items or retention finalizers remained active.

The response-loss case injected an adapter error after the scheduler accepted
real deletion. It was not a network partition or an uncatchable worker crash.
The hardware work was assistant-executed. See the [sanitized receipts and checks](evidence/termination-retention.json).
