# E6 two-project acceptance plan

Status: frozen before new trial Jobs. This is deterministic policy acceptance,
not a statistical performance comparison or a general tenant-security claim.

## Question, scope and actors

Can two authenticated projects submit real GPU work through the existing API and
worker while preserving namespace routing, quota, priority and project-owned
results/usage? Project A uses the existing lab namespace; project B gets a new
isolated namespace, LocalQueue and API identities. Researchers receive project
API credentials, not direct Kubernetes write credentials. One trusted platform
worker services both routes, with explicit namespace RoleBindings.

Both LocalQueues use the existing idle one-GPU ClusterQueue: CPU quota 2,
memory quota 2 GiB, GPU quota 1, BestEffortFIFO and no preemption. Those settings
must not change. Each namespace additionally caps requested physical
GPUs at one. These are per-project ceilings sharing one aggregate pool, not
independent one-GPU entitlements or a fairness guarantee.

Use the qualified immutable CUDA recovery fixture, same GPU/image/package tree,
shape, seed, precision and 30-second preparation wait. Read-only runtime packages
may be exposed through a separate site-owned PVC in B; no training data or
credentials are shared through it. Qualify that new namespace/runtime binding
before application trials. No node, driver or existing application changes.

## Starting conditions and budget

- API, worker, PostgreSQL, artifact delivery and MLflow healthy; no active
  platform Job or outstanding outbox event; shared GPU queue idle.
- GPU node Ready with no pressure; image, source and environment bindings match
  the qualified fixture. Existing A qualification may be reused only while its
  scope, inputs and validity remain unchanged.
- Route-owned normal/high WorkloadPriorityClasses have values 100/1000. No Pod
  PriorityClass, preemption or borrowed GPU quota is introduced.
- Bound: one new B qualification, six application GPU Jobs, two oversized
  suspended Jobs, API/RBAC/quota negative checks. Application Jobs retain their
  180-second execution and 300-second queue limits. No automatic repeat on failure.
- Keep failed/partial runs and all costs. A timed-out observer must inspect its
  saved IDs and current scheduler state rather than submit replacements.

## Scenarios and observable acceptance

1. **Credentials and routing.** Register operator-qualified project-owned
   contracts. Submit with researcher credentials. Assert namespace, LocalQueue,
   Job annotations, Workload owner UID, node selector and physical GPU request.
   The same idempotency key in different projects creates separate attempts;
   retries within each project return its original attempt.
2. **Scope rejection.** Each project tries the other's workload, Job read/cancel,
   and later artifact content: 404 and no mutation. A client-supplied namespace,
   queue or project override in JobRequest must be rejected. Lists, overview and
   usage expose no foreign records. Runner Pods mount no ServiceAccount token.
3. **Quota boundaries.** A two-GPU candidate exceeds actual node capacity and is
   rejected before scheduler submission. Server-side dry-run two-GPU Pods in
   both namespaces must fail ResourceQuota admission. A valid one-GPU candidate
   asking for three CPUs must remain suspended at the two-CPU Kueue quota, with
   QuotaReserved=False and no Pod; cancel through the API and retain its ledger.
   Missing allocation timestamps must stay unknown, not be fabricated as zero.
4. **Priority, round 1.** Start A normal as a holder. While it holds the GPU,
   submit B normal and then A high. Both wait. On holder completion, A high is
   admitted while older B normal remains pending. Both eventually validate.
5. **Priority, round 2.** Reverse project roles: B holder, A normal, B high.
   This checks both project routes; it is not a replicated fairness experiment.
6. **Trace and cleanup.** Each of the six application executions has exactly one
   result, usage row and MLflow run in its project's experiment. Compare S3,
   authenticated API and MLflow artifact bytes. Capture actual queue/Pod times
   and GPU reservations, including qualification and failed/cancelled attempts.
   End with no active trial Jobs, pending cleanup/delivery or retained finalizers.

The verifier may cancel only its recorded unfinished attempts through their own
project APIs. It must not delete namespaces, change quota or clear pressure
taints to obtain a pass. Submitted Jobs and terminal records remain audit evidence.

## Reproduction contract

`examples/verify_project_isolation.py` executes the bounded API/Kueue scenarios.
It does not provision credentials, qualify hardware or expand quota. Prepare both
routes, immutable project-owned workload/variant/capability registrations,
normal/high priority mappings, MLflow experiment mappings and artifact bucket
mappings before invoking it. Shared storage requires project-prefixed keys and
API ownership checks; this is not direct storage-service tenant authorization.
Run the [local deployment preflight](configuration-checks.md) on both API and
worker files before updating service configuration.

The optional `deploy/projects/lab-project-b.yaml` assumes the existing shared
ClusterQueue. Its namespace must also match the installed controller's
`managedJobsNamespaceSelector` and webhook namespace selectors. Supply those
site-specific labels in a private overlay; matching the ClusterQueue selector
alone is insufficient. Kubernetes extended-resource quotas use only
`requests.nvidia.com/gpu` (not `limits.nvidia.com/gpu`).

Create a private JSON configuration, outside the public checkout, with this
shape; supply both `a` and `b` project entries with independently scoped tokens:

```json
{
  "api_url": "https://advisor.example.invalid",
  "ca_file": "/private/ca.crt",
  "run_ref": "unique-predeclared-trial-id",
  "cluster_queue": "shared-gpu-pool",
  "projects": {
    "a": {
      "project_ref": "project-a",
      "token": "<private researcher token>",
      "namespace": "project-a",
      "local_queue": "research",
      "candidate_ref": "gpu-one",
      "workloads": {
        "normal": "qualified-a-normal",
        "high": "qualified-a-high",
        "oversized_cpu": "qualified-a-cpu-over-quota",
        "oversized_gpu": "qualified-a-gpu-over-capacity"
      }
    }
  }
}
```

```sh
python examples/verify_project_isolation.py \
  --config /private/e6-config.json --report /private/e6-trial.json
```

The report must not exist. It is private and records submitted identifiers before
dependent checks. On failure, inspect those same attempts and retained scheduler
evidence; do not rerun with a new report to erase a failed trial. The verifier's
PASS covers its scenarios only. Artifact byte comparison, ledger uniqueness,
MLflow run uniqueness, RBAC/quota dry-runs and final cleanup need separate recorded
verification before claiming the full acceptance gate.

## Limits

This covers the project API boundary and trusted route mapping on one GPU. It
does not grant researchers direct MLflow/S3 administrative access or establish
those services' complete tenant authorization. It does not test Slurm multi-user
policy, per-project worker-process isolation, large-cluster fairness, adversarial
administrators or network-policy isolation. No speedup or useful GPU-utilization
claim follows from admission order or reservation seconds.
