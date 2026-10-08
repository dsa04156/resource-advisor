# Lab quota and priority verification

On 2026-10-02, actual Slurm 24.11.5 submissions verified account limits and QOS
priority on the qualified Orin Nano GPU. [Raw, sanitized evidence](../evidence/slurm-policy.json)
includes successful and cancelled allocations, timestamps and rejection messages.
This is a scheduling acceptance trial, not a GPU performance comparison.

## Configured policy

Only the root and dedicated lab account existed; the queue was empty before the
change. The controller configuration and original associations/QOS were backed up.
The existing partition, nodes and worker drivers were retained. The controller was
reconfigured without restarting or enabling preemption.

| Setting | Value |
|---|---|
| AccountingStorageEnforce | associations,limits,qos |
| PriorityType / PriorityWeightQOS | priority/multifactor / 1000 |
| Account aggregate limit | 1 CPU, 1 GiB memory, 1 GPU |
| Allowed QOS | ra-normal, ra-high; default ra-normal |
| QOS priorities | 10 and 100; actual job priorities were 100 and 1000 |
| Per-job limit for either QOS | 1 CPU, 1 GiB memory, 1 GPU, 2 minutes |
| QOS flags | DenyOnLimit |

The enforcement switch is **controller-wide**. It was applied only after checking
all existing associations in this separate lab cluster. On a shared deployment,
audit every existing user's association/default QOS before enabling it. The lab
account cannot request the unrestricted `normal` QOS. This trial uses one OS user;
it does not establish isolation between different users or device-level cgroups.

## Observed behavior

1. A two-CPU request failed with `QOSMaxCpuPerJobLimit`.
2. A 2 GiB request failed with `QOSMaxMemoryPerJob`.
3. A three-minute request failed with `QOSMaxWallDurationPerJobLimit`.
4. An unassigned QOS request failed with `Invalid qos specification`.
5. With one GPU-reserved blocker running, normal/high jobs both waited with
   `AssocGrpCpuLimit`. The selected node has six CPUs; this is an account policy
   limit, not evidence that all physical CPUs were occupied.
6. The normal job was submitted first. After cancelling the blocker, the high job
   started at 05:48:38 UTC; the normal job started at 05:48:41 UTC. Both reserved one
   GPU and completed the driver-only CUDA probe with exit `0:0`.
7. The controller remained UP, both nodes returned idle and the queue was empty.

The first verifier expected a differently capitalized reason string and timed out.
The second failed on `sprio`, which reported an invalid job ID despite a pending
`scontrol` record. Both trials cancelled only their own jobs. The successful trial
still records that diagnostic; individual priority factors remain unavailable.
Priority ordering is proved by `scontrol` values and actual accounting timestamps.

Only one physical GPU exists in this tested pool. An oversized two-GPU request
cannot independently establish quota enforcement rather than insufficient physical
capacity. The tested per-job CPU/memory/time limits distinguish policy from capacity.
Allocated GPU time is not measured GPU utilization.

## Repeat in an already configured lab

Install the [CUDA driver probe](../../examples/cuda_driver_probe.py) on the worker and
verify its digest. On a Slurm client/controller with the intended account:

```sh
python examples/verify_slurm_policy.py \
  --account research-lab --partition gpu --node qualified-gpu-node \
  --gres gpu:verified-model:1 --normal-qos ra-normal --high-qos ra-high \
  --probe /opt/research/cuda_driver_probe.py
```

Use the actual verified partition/node/GRES, not these placeholders. The script
requires the policy above and an idle account, bounds every job to two minutes,
retains diagnostics, and cancels only IDs it submitted. The probe must already be
accessible on the worker; this does not assume the controller's files are shared.

For a fresh dedicated lab, an administrator can create the two QOS records with
`sacctmgr add qos`, set their `Priority`, `MaxTRESPerJob`, `MaxWall` and
`Flags=DenyOnLimit`, then set the lab account/user's allowed/default QOS and account
`GrpTRES`. Back up configuration and association output first. Enable
`AccountingStorageEnforce=limits,qos` and `PriorityWeightQOS=1000`, then reconfigure
and verify the effective configuration. This is not an unattended production
installation procedure. Rollback must restore both configuration and association
state; restoring only `slurm.conf` does not undo database policy changes.

## Application mapping

`WorkloadSpec.execution.priority` accepts `normal` or `high`, defaulting to normal.
The project/cluster route maps that grade into `qos_by_priority` for Slurm and
`priority_classes` for Kueue. See [route examples](../../deploy/routes.example.json).
Kueue uses its workload-priority label; Pod priority is unchanged. Each configured
class/QOS must already exist and be authorized. An unmapped high request fails
before backend submission. No application-supplied numeric scheduler priority is
accepted. Route mapping is tested separately from this direct-`sbatch` live trial.

### Scoped gateway priority configuration

The root-owned forced gateway defaults to its single configured `qos`. To enable
both API priority grades, configure `qos_by_priority` in **both** the worker route
and controller gateway scope, for example:

```json
{"qos":"research-normal","qos_by_priority":{"normal":"research-normal","high":"research-high"}}
```

These QOS records must already exist and be authorized for that route's Slurm
user/account. The gateway accepts only the default QOS and the administrator's
explicit normal/high mapping. It rejects unknown grades, unsafe values,
inconsistent normal defaults and unlisted QOS. Account, partition, Linux owner,
resource ceiling and qualified native runtime restrictions remain in force.
Restart an idle dedicated worker after changing its route; running workloads must
not be interrupted to refresh configuration. Configuring the map does not create
QOS, change scheduler priority weights or override native admission limits.

The subsequent [actual high-profile API trial](slurm-priority-gateway.md) now
verifies plan → scoped gateway → ra-high/priority1000 → native GPU CNN → durable
result/ledger/MLflow with 72 GPU reservation seconds. This is end-to-end mapping
evidence; two-project ordering/fairness remains a separate unverified gate.

References: [Slurm QOS](https://slurm.schedmd.com/qos.html),
[Slurm limits](https://slurm.schedmd.com/resource_limits.html),
[Kueue WorkloadPriorityClass](https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/).
