# Virtual Slurm route `virtual-slurm-140` (read-only inventory)

A second instance of the [Slurm inventory collector](../slurm-inventory/README.md)
for a user-supplied **simulated** Slurm 24.11.5 cluster (Docker containers:
`slurmctld` plus nodes `cpu-1`, `cpu-2`, `gpu-big`, `gpu-mid`, `gpu-s1`, `gpu-s2`).
It writes `cluster_ref: virtual-slurm-140` into project `ra-lab`, so the existing
combined resource list shows it beside Kubernetes. The paused `lab-slurm` route and
its history and the `live-vm-slurm` execution route are separate and unchanged.

## Honesty boundary

- The controller reports `gpu:a100` GRES (4/2/1/1 = 8 slots), but the devices are
  `/dev/null` mapped to `/dev/nvidiaN`. The route classifies `gres/gpu` as
  `device_class: gpu, allocation_mode: virtual_slot`; it is never a physical device,
  and no A100 performance, model or execution qualification follows.
- Node aliases carry the provenance prefix `virtual-slurm-140-`.
- No Prometheus host bindings: snapshot status is `partial` with
  `prometheus: not_configured`; CPU/memory are scheduler registrations, not
  measured host usage.

## Execution is not validated

Accounting (SlurmDBD) was added on 2026-10-29, see
[`../slurm-accounting-virtual-140`](../slurm-accounting-virtual-140/README.md). The execution
route in [`../slurm-execution-virtual-140`](../slurm-execution-virtual-140/README.md) is
configured but `NOT_VALIDATED`: no qualified native runtime, worker at 0 replicas, no job
submitted through Resource Advisor.

## Transport

The collector uses SSH transport to a forced-command key on the controller host's
existing operator account (`restrict`, `from=` the observed pod egress address,
pinned ed25519 host key). The forced command is
[`examples/slurm_observer.py`](../../examples/slurm_observer.py) with scope

```json
{"account": "ra-lab", "partition": "all",
 "nodes": ["cpu-1", "cpu-2", "gpu-big", "gpu-mid", "gpu-s1", "gpu-s2"],
 "container": "slurmctld"}
```

`container` makes the gateway run `docker exec --user slurm slurmctld
/usr/bin/{scontrol,squeue} ...`; the host's own native Slurm client is never invoked.
The gateway files are owned by that operator account (mode 0600 in a 0700
directory), not root: this is a command boundary for one trusted account, not a
root-owned isolation boundary. Because the node TRES omits `gres/gpu`
(`AccountingStorageTRES` lacks it), the collector reads the configured count from
the node's `gres` registration.

Private inputs, kept outside Git: Secret `ra-virtual-slurm-140-observer-ssh`
(`config`, `id_ed25519`, `known_hosts`, SSH config `Host ra-virtual-slurm-140-observer`)
and Secret `ra-virtual-slurm-140-inventory-config` (`inventory.json`). The site
overlay pins the image digest and node selector.

Rollback: scale `ra-virtual-slurm-140-inventory` to 0, delete the two Secrets, and
remove the single `ra-virtual-slurm-140-observer` line from the account's
`authorized_keys`. Snapshots already stored remain as history.
