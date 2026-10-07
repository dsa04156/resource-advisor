# Common heterogeneous GPU pool

Researchers request a registered workload and its requirements. The platform
chooses a compatible candidate; they do not need to select an individual GPU.
Kubernetes routes in the isolated platform project share the native ClusterQueue
`hairp-gpu-pool`. Existing LocalQueue names remain adapter bindings and point at
this ClusterQueue. The separate two-GPU queue is retained only for quota comparison.

The pool does not make unlike accelerators interchangeable:

| Native resource/flavor | Example capacity | Meaning |
|---|---|---|
| `nvidia.com/gpu`, `hairp-multi-gpu` | 3 exclusive devices on three qualified CUDA nodes | RTX and ARM GPU variants still require compatible image/runtime contracts |
| `nvidia.com/gpu.shared`, two Jetson flavors | 2 logical slots per node | Four logical slots on two physical GPUs; never four extra physical GPUs |
| Slurm GPU inventory | Backend-native GRES | Same logical platform inventory; Slurm admission/accounting remains separate |
| NPU inventory | Vendor-specific resources | Separate device type and qualified model/runtime; not CUDA GPU capacity |

`examples/scheduler_lab/gpu-pool-resources.json` is a lab example. It depends on the
existing qualified `hairp-multi-gpu` ResourceFlavor and `hairp-lab-hostname` Topology
from the multi-GPU lab setup. Replace Jetson label selectors with reviewed existing
lab labels; never relabel production nodes merely to apply the example. Quotas are
lab ceilings, not total hardware discovery. Recheck CPU/memory budgets against your
registered workloads before enabling routes.

A single wide CUDA flavor permits a two-worker IndexedJob PodSet to request two
GPUs across nodes. Splitting it into three per-node flavors with quota one would
prevent that PodSet from receiving a two-GPU flavor assignment. Hostname topology
still records the domains. Jetson shared flavors remain distinct because their
resource names and runtime contracts differ. The native semantics are defined in
the [Kueue ClusterQueue documentation](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/).

LocalQueue `spec.clusterQueue` is immutable on the installed CRD. To move existing
lab routes, first check their queues have no unfinished Workloads, preserve metadata,
then recreate only those empty LocalQueues under their existing names. Do not delete
active queues or use force deletion. Other projects' queues and quotas remain intact.
The inventory collector needs named read access to the new ClusterQueue and an
explicit configured reference; its schema continues distinguishing reservation,
admission, scheduler request headroom and measured utilization.

Automatic selection counts accepted nonterminal requests within the same project
and uses that demand as a tiebreaker after compatibility and native availability.
For equal active demand, five-minute assignment counts also include completed
requests. This prevents fast jobs from always returning to the first candidate
while a concurrent burst is still being accepted. Counts are exposed in each
selection evaluation and are not physical allocations.
A PostgreSQL advisory transaction lock serializes automatic selection and acceptance,
so simultaneous arrivals see earlier accepted requests. The native scheduler still
owns admission and resource allocation. This is neither a global distributed GPU
lease nor quota enforcement across Slurm and Kubernetes. There is no new runtime
upgrade, physical GPU pooling, cross-scheduler DDP, or automatic model qualification.

The ten-request registered runner allows 60 seconds for serialized acceptance.
Transient receipt failures retry the identical request and idempotency key at most
three times. The board keeps all ten requested rows from submission onward; a
requested row is not native admission evidence. Successful receipts are retained
for cleanup even if another submission fails. Process death or exhaustion of all
receipt retries still requires reconciliation; this is not crash-proof ownership.

When native Slurm node identities differ from inventory display aliases, the
operator config declares `gpu_pool.node_aliases` explicitly. The node table and
job selection use this display mapping; stored native evidence stays unchanged.
An actually executing registered job also proves its current participation even
if that route is absent from the submission catalog.

The lab namespace ResourceQuota must also agree with native admission. An older
`project-gpu-ceiling` restricted `requests.nvidia.com/gpu` to one, although the
common ClusterQueue admitted three exclusive devices. Long jobs exposed this:
Kueue admitted the second job, then Kubernetes refused its Pod with `FailedCreate`.
The example now includes a ceiling of three in `resource-advisor-lab`. Apply this
only to the owned lab namespace after inspecting existing quotas; other projects
keep their own budgets. Logical shared slots and NPU resources remain distinct.
