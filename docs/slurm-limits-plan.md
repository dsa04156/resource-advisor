# Standalone Slurm worker enforcement acceptance plan

The [first Orin CNN result](slurm-torch-results.md) exceeded its host-memory
request while `TaskPlugin` was unset. This follow-up tests actual job limits;
it does not retry that CNN or promote its warning-bearing PyTorch environment.

## Configuration scope

Use the separate [Ansible play](../automation/ansible/slurm-limits.yml) only on the
approved Orin lab worker. The existing telemetry play remains read-only for Slurm.
The new play requires Slurm 24.11.5, cgroup-v2 memory/CPU/cpuset controllers,
an empty lab queue, an IDLE allocation-free selected node, absence of active
Kubernetes/KubeEdge services, and the exact reviewed configuration hash. It refuses
existing unmanaged limit configuration. The Pi currently has no memory controller
and must fail preflight without mutations.

On first adoption, drain only the selected idle Slurm worker, recheck allocations,
retain its original configuration, install the task/cgroup, task/affinity and
jobacct_gather/cgroup settings, and restart only that drained worker daemon.
Release only the play's own drain after healthy registration. Preserve controller
configuration, other workers, GRES mapping, driver files and boot identity. On a
failed application keep the failure evidence and inspect the drain/configuration;
do not blindly replay or resume the node. The saved original is a recovery aid,
not evidence that rollback occurred.

The existing GPU device mapping remains unchanged. Enabling `ConstrainDevices`
alone does not prove that Jetson's alternate device interfaces cannot bypass it;
the positive/negative kernel checks below determine the bounded result.
No direct-SSH containment or general multi-user security claim follows from them.

## Fixed three-Job acceptance

Publish the [probe source](../examples/slurm_limits_probe.py) and source hashes
before submission. Under the existing lab account and normal QOS, submit these
Jobs sequentially, one CPU each and a two-minute limit:

1. **GPU positive:** one typed Orin GPU, 512 MiB RAM. Require the actual inherited
   cgroup memory maximum to equal 512 MiB, swap maximum zero, one CPU in task
   affinity, and the existing driver-only CUDA kernel's 4,096-element PASS.
2. **GPU negative:** no GPU, 256 MiB RAM. Require the same limit/affinity checks,
   opening `/dev/nvidia0` to fail specifically with EPERM, and that same CUDA
   probe to refuse initialization/device/context creation. Successful kernel
   execution, unrelated failures or absent driver libraries fail acceptance.
3. **Memory overrun:** no GPU, 64 MiB RAM. Before allocating, require inherited
   `memory.max=67108864`, zero swap allowance and one CPU affinity. Only then
   attempt at most 160 MiB of page-touched memory within that job cgroup. Require
   Slurm to record `OUT_OF_MEMORY`; retain the pre-allocation marker and terminal
   accounting. This is an allocation-contained failure, never host-wide stress.

Stop on any unexpected result, retain every attempt/cost and inspect the same
external ID after observation timeouts. No retry-until-pass. Maximum reservation
budget is 120 GPU-seconds and six allocation-minutes; actual allocation intervals
come from accounting. The memory negative test must not run if its finite kernel
cap cannot be read and verified. Independent software tests cover ancestor limit
inheritance, missing/unlimited bounds and refusal outside a Slurm allocation.

Afterward require an empty queue, the worker back to IDLE, preserved protected
identities and an Ansible repeat with `changed=0` and no daemon restart. Check
mode verifies preconditions only and deliberately skips mutations; it is not a
deployment or hardware acceptance claim. Keep initial preflight failures in the
report. The first check found an Ansible 2.19 strict-boolean conditional error,
with `changed=0`; the condition was corrected instead of weakening the guard.

## Reproduction

Supply private inventory with groups `ra_slurm_limit_nodes` and a controller host.
Set `ra_lab_authorized=true`, `ra_existing_production=false`,
`ra_slurm_limits_authorized=true`, `ra_slurm_controller` to the controller inventory
alias, `ra_slurm_node_name` to the selected scheduler node, and
`ra_slurm_original_sha256` to its reviewed original `slurm.conf` hash. Retain strict
host-key checking and private credentials, using the pinned Ansible environment
described in [the provisioning guide](../automation/ansible/README.md).

```sh
ansible-playbook -i /secure/limits-inventory.json automation/ansible/slurm-limits.yml --check
ansible-playbook -i /secure/limits-inventory.json automation/ansible/slurm-limits.yml
```

The second command changes the approved worker's Slurm configuration and restarts
its idle daemon; it is distinct from the existing telemetry provisioning command.
The receipt pins managed file hashes. External configuration drift requires review
before another apply; the play does not overwrite another manager's changes.

Semantics are checked against the installed-version
[cgroup.conf manual](https://github.com/SchedMD/slurm/blob/slurm-24-11-5-1/doc/man/man5/cgroup.conf.5)
and [GRES manual](https://github.com/SchedMD/slurm/blob/slurm-24-11-5-1/doc/man/man5/gres.conf.5).
