# Slurm limits v1: actual GPU bypass detected

The [fixed three-Job plan](slurm-limits-plan.md) stopped after its second Job.
The Orin worker now sets real cgroup memory/swap/CPU bounds, but its existing
GPU GRES mapping does **not** provide CUDA isolation. Preserve this failed
acceptance result and do not register isolated automatic execution.

| Probe | Observed result |
|---|---|
| Reserved GPU, 512 MiB | `memory.max=536870912`, swap zero, one CPU affinity; 4,096 CUDA outputs verified; completed |
| No GPU, 256 MiB | Correct memory/swap/CPU bounds and `/dev/nvidia0` denied with EPERM, **but the CUDA kernel still executed**; verifier correctly failed |
| 64 MiB contained overrun | Not submitted because the preceding acceptance failed |

[Raw sanitized evidence](evidence/slurm-limits-v1.json) retains both allocations,
their logs and step accounting: one GPU reservation second, four total allocation
seconds. The second Job ran real GPU work without a recorded GPU allocation;
actual total GPU busy time is unknown. Do not interpret zero reservation on that
Job as zero physical GPU use. Fast-step MaxRSS is missing where accounting did
not report it; those values are not zero-filled.

Ansible applied the explicit worker-only change, then returned the node to IDLE.
Controller configuration, the other worker, QOS/associations, driver files, GRES
and boot identities were preserved. Initial check mode found a strict-boolean
expression error with zero changes; corrected check mode passed. The unapproved
inventory guard also rejected with zero changes. No host reboot or driver update
occurred. Full zero-change repeat and OOM acceptance remain to be checked.

The existing GRES points to `/dev/nvidia0`; its denial is directly observed, so
this is not merely a missing `ConstrainDevices` flag. The successful unreserved
kernel requires investigating which Jetson interface the driver actually opens.
The [separate one-Job trace plan](slurm-device-trace-plan.md) reserves a GPU while
collecting that evidence. It does not replay the failed v1 acceptance or discard
its cost. No speculative alternative device mapping is installed yet.
