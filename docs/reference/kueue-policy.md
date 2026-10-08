# Live Kueue GPU queue acceptance

The 2026-10-02 trial used the existing Kubernetes v1.31.14 / Kueue v0.19.5
installation. No Kubernetes/KubeEdge upgrade, controller rollout, pressure-taint
removal or existing application change was made. The selected GPU node was Ready,
had no pressure conditions, and exposed one allocatable GPU. The immutable CUDA
source was compared with this repository before submitting any work.

The dedicated lab already had a namespace-scoped LocalQueue, a ClusterQueue limited
to selected lab namespaces, and a ResourceFlavor selecting the qualified GPU node.
The queue's nominal quota was 2 CPUs, 2 GiB memory and one physical GPU, with
BestEffortFIFO and all preemption disabled. The queue was idle before the trial.
Two explicitly selected [WorkloadPriorityClass objects](../../deploy/kueue-priorities.yaml)
were added with values 100 and 1000. They are not global defaults and do not change
Pod priority. Existing cluster policy continues to determine namespace management.

## What happened

| Trial | Actual outcome |
|---|---|
| Request two GPUs from a one-GPU quota | Job creation succeeded; QuotaReserved=False with an insufficient-quota reason; Job stayed suspended and no Pod existed |
| Hold one GPU, submit normal then high | Both waited with insufficient unused GPU quota; their Workload priorities were 100 and 1000 |
| Delete only the trial's holder | High became admitted while the older normal job remained unadmitted |
| Run the two jobs | High Job started 05:57:21 UTC; normal Job started 05:57:35 UTC; both completed real CUDA F0 validation of 4,096 values |
| Clean up | Only this invocation's Jobs were deleted; no cleanup error; pending/admitted/reserving counts all returned to zero |

See [the sanitized conditions, timestamps and CUDA output](../evidence/kueue-policy.json).
Kueue's oversized behavior is **pending admission**, unlike the tested Slurm QOS
submission rejection. The platform must preserve that distinction. A GPU
reservation is not GPU utilization, and this is not a model throughput measurement.
The deliberate holder cancellation is not counted as a completed benchmark.

## Reproduction

Use a dedicated idle lab queue, an already-qualified GPU node/image, and the
[CUDA source](../../examples/hardware_probe.cu). The existing cluster must serve the
API version used by the priority manifest; inspect the CRD before applying it.
For older clusters use a compatible served API/release or a separate management
environment. Do not upgrade the running cluster just for this check.

Prepare a private, create-ready JSON `batch/v1` Job template with:

- One container whose command is `bash -c` followed by compiling and executing the
  CUDA probe. It must fail on a bad result and emit the F0 JSON record.
- A pinned qualified image, read-only immutable source ConfigMap, explicit resource
  requests/limits and qualified-node selector. No `nodeName`, Pod PriorityClass,
  init containers, or unexpected sidecar injection.
- No mounted service-account token, no privilege escalation and dropped capabilities.

Then use existing authorized kubectl credentials:

```sh
kubectl create -f deploy/kueue-priorities.yaml
python examples/verify_kueue_policy.py \
  --template /private/qualified-cuda-job.json \
  --namespace research-lab --queue gpu-lab --cluster-queue gpu-lab \
  --report /private/kueue-policy-run.json
```

Replace the queue/namespace placeholders with the isolated lab names. Do not run
`create` again if those priority classes already exist; inspect ownership and values
first. The verifier mutates only new trial Jobs; it does not edit the queue or node.
Each admitted Job has a 180-second active deadline. Observation waits are bounded;
its `finally` cleanup deletes only recorded trial Job names, never a namespace or
other workloads. A killed client cannot guarantee cleanup: inspect names in its
private report and current cluster state before deleting or retrying. Suspended
Jobs do not consume the active execution deadline. Reports contain site identifiers
and must be sanitized before publication; use a new report path for each trial.

The application maps normal/high into separate route-owned Kueue class names and
Slurm QOS names. This trial exercises direct Job admission, independently of the
tested API adapter mapping. A live KFP → Compute API chain, cross-project quota
sharing/identity isolation and large-cluster fairness remain separate acceptance
gates. An arbitrary user with direct cluster write permissions can choose a
different workload label; project API authentication is not a replacement for
Kubernetes RBAC/admission policy.

References: [Kueue WorkloadPriorityClass](https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/)
and [ClusterQueue](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/).
