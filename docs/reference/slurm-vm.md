# Dedicated Slurm lab VM

A real QEMU virtual machine that runs Slurm for Resource Advisor integration.
It is a **single-node, CPU-only lab**: one guest runs `slurmctld`, `slurmd`,
`slurmdbd` and MariaDB. It has no GPU/NPU passthrough and advertises no GRES.
It does not replace or repoint the physical Slurm site (`192.168.0.12`).

The host has `/dev/kvm` but this account cannot open it, and no root access was
used. QEMU therefore runs with **TCG software emulation**. Timings from this VM
are not performance evidence for any hardware.

## Layout

| Item | Value |
|---|---|
| Hypervisor | `qemu-system-x86_64` 8.2.2 (Ubuntu `1:8.2.2+ds-0ubuntu1.18` debs extracted to a private directory), `-accel tcg,thread=multi`, `-cpu max`, q35 |
| Guest | Debian 13 (trixie) official `debian-13-genericcloud-amd64.qcow2`, SHA-512 checked against `cloud.debian.org` `SHA512SUMS`, cloud-init NoCloud seed |
| Resources | 2 vCPU, 4096 MiB RAM, sparse 16 GiB qcow2 overlay (base image kept read-only) |
| Slurm | 24.11.5 (`24.11.5-4+deb13u1`), munge 0.5.16, MariaDB 11.8.6, `slurmdbd` accounting |
| Cluster / partition | `ra-vm` / `compute`, node `ra-slurm-vm` (`CPUs=2 RealMemory=3072`) |
| Account / QOS | `ra-project` / `ra-normal` (`MaxWall=01:00:00`, `MaxTRESPerJob=cpu=2`) |
| Runtime | `/usr/bin/python3` 3.13.5, `python3-numpy` 2.2.4 (Debian packages) |
| Job users | `ra` (password-locked, no sudo, job owner); `raadmin` (sudo, operator only) |
| Network | QEMU user networking; only guest port 22 forwarded to `127.0.0.1:2232` |

Policy: `select/cons_tres` with `CR_Core_Memory`, `task/cgroup` +
`proctrack/cgroup` (cores and RAM constrained),
`AccountingStorageEnforce=associations,limits,qos`, and
`job_container/tmpfs` giving each job a private `/tmp` and `/dev/shm`.
The RA inventory data parser `v0.0.42` works with `scontrol`/`squeue --json`.

## Scripts (`deploy/slurm-vm/`)

```sh
deploy/slurm-vm/provision.sh   # once: debs, image check, keys, seed, overlay disk
deploy/slurm-vm/start.sh       # boot in background (first boot installs Slurm, ~13 min under TCG)
deploy/slurm-vm/status.sh      # QEMU pid, guest OS, service states, sinfo
deploy/slurm-vm/smoke.sh       # bounded numpy CPU job as ra, waits for sacct COMPLETED
deploy/slurm-vm/ssh.sh [ra|admin] [cmd]
deploy/slurm-vm/stop.sh        # ACPI poweroff of this VM only; disk and keys kept
deploy/slurm-vm/install-gateway.sh <slurm_executor.py> <config.json>
```

Nothing deletes the disk. To re-provision from scratch, stop the VM and remove
`.state/slurm-vm-live/disk/` by hand. Settings (`SLURM_VM_BIND`,
`SLURM_VM_SSH_PORT`, CPU/memory/disk) are environment overrides read by
`common.sh`. Binding a LAN address for in-cluster workers is an explicit operator
choice; the default is loopback.

## Private state (`.state/slurm-vm-live/`, mode 700, not committed)

| Path | Contents |
|---|---|
| `keys/known_hosts` | pinned guest ed25519 host key (generated before first boot, injected by cloud-init) |
| `keys/ra_ed25519`, `keys/admin_ed25519` | operator shell keys for `ra` and `raadmin` |
| `keys/ra_gateway_ed25519` | key that can only run the forced-command gateway |
| `ssh_config` | aliases `ra-slurm-vm`, `ra-slurm-vm-admin`, `ra-slurm-vm-gateway` |
| `run/qemu.pid`, `run/monitor.sock`, `logs/console.log` | process, QEMU monitor, serial console |
| `seed/` | cloud-init seed (contains the guest host private key) |
| `versions.txt`, `debs/SHA256SUMS` | exact hypervisor packages and image checksum |

The MariaDB password and munge key are generated inside the guest and never
copied to the host.

## Execution boundary for Resource Advisor

`install-gateway.sh` installs the gateway script at `/opt/ra/slurm_executor.py`
and its configuration at `/etc/ra/slurm-executor.json`, both root-owned. User
`ra`'s SSH keys are served from root-owned `/etc/ra/ssh/ra.authorized_keys`
(`sshd` `Match User ra`), so a job running as `ra` cannot remove the
`restrict,command=...` binding of the gateway key. The installer was checked with
the committed gateway (`75136d4`): allowed `squeue`/`sacct` forms succeed, and
`id` is rejected.

For training inputs, `/opt/ra`, `/srv/ra/inputs` and `/etc/ra` are root-owned
and read-only to jobs. `/srv/ra/work` is owned by `ra` (mode 700) for outputs.
Two concurrent jobs each saw only their own `/tmp` marker.

Limits that remain true:

- All RA jobs share the UID `ra`. Private `/tmp` does not separate their writable
  files under `/srv/ra/work`, and `kernel.yama.ptrace_scope` is `0`. Per-attempt
  isolation must come from a root-owned runner (for example, a per-attempt copy
  into the job-private `/tmp` and a validated copy out). That runner is not
  installed here.
- One node, CPU only, TCG emulation. The configuration does not demonstrate
  multi-node placement, GPU/NPU allocation or failover.

## Verified on 2026-10-28

- QEMU process `ra-slurm-vm` running under the host user; guest reports
  `Debian GNU/Linux 13 (trixie)`, kernel `6.12.113+deb13-cloud-amd64`.
- SSH succeeded with `StrictHostKeyChecking yes` against the pre-pinned host key.
- `munge mariadb slurmdbd slurmctld slurmd` active; `sinfo` shows
  `ra-slurm-vm compute idle 2 3072`.
- `sbatch` job 1 (`ra-project`/`ra-normal`, 1 CPU, 256M, 5 min limit) reached
  `COMPLETED 0:0`. `sacct` listed the job and its `batch` and `0` steps.
  The numpy least-squares run printed `numpy=2.2.4 python=3.13.5 steps=300`.
- After `stop.sh` and `start.sh`, `sacct` still listed jobs 1–3, and job 4
  completed. `slurmdbd` becomes reachable a few seconds after `sinfo` reports
  idle under TCG.

This is integration infrastructure, not a completed RA Slurm training path. RA
training on this VM still needs the application-side runner, route and qualification work.
