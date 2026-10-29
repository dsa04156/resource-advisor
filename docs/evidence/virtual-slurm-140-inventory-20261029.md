# virtual-slurm-140 inventory connection — 2026-10-29

Levels: code + focused tests passed; deployed in the lab namespace; observed through
the live API and console. **Not** physical GPU evidence, **not** execution qualification.

| Item | Result |
|---|---|
| Controller | Docker `slurmctld`, Slurm 24.11.5, `data_parser/v0.0.42`; queried only via `docker exec --user slurm slurmctld` (host native Slurm never called) |
| Route | project `ra-lab`, `cluster_ref: virtual-slurm-140`, Deployment `ra-virtual-slurm-140-inventory` (1 replica), source commit e371ed3 |
| Snapshot (07:06:50Z) | `slurm_nodes: ok`, `slurm_queue: ok` (account `ra-lab`/partition `all` records only = 0; not a whole-cluster queue), `prometheus: not_configured` → status `partial` |
| Nodes | 6 × IDLE: `virtual-slurm-140-{cpu-1,cpu-2}` 4 CPU/2000 MiB, `gpu-big` 4 CPU, `gpu-mid` 4 CPU, `gpu-s1`/`gpu-s2` 2 CPU/1000 MiB |
| GRES | `gres/gpu` `device_class: gpu`, `allocation_mode: virtual_slot`: 4/2/1/1 = 8 slots, 0 reserved. Devices are `/dev/null` mapped as `/dev/nvidiaN`: no physical GPU, no A100 performance claim |
| Combined list | API overview: `lab-kubernetes` (12 nodes, live), `lab-slurm` (2 nodes, stale since 2026-10-22, history retained), `virtual-slurm-140` (6 nodes). Console 자원 view: "3 clusters", six SLURM tiles, slots shown as "공유 슬롯" |
| Transport boundary | Forced-command key on the existing operator account: `restrict`, `from=` pod egress, pinned ed25519 host key. Allowed: the two JSON reads. Denied: `id`, `bash -i`, `sinfo`, `docker ps`, `sbatch`, chained `; id`, foreign account, no command, TCP forwarding (administratively prohibited). Gateway is operator-account owned (0700/0600), not root-owned |
| Review | Independent spec + standards review; P2 (malformed TRES falling back to GRES capacity) fixed in e371ed3 with a 4-case regression that fails on e408087 |

## Not done / limitations

- **Execution unavailable:** `AccountingStorageType=(null)`, no slurmdbd. `SlurmBackend`
  decides completion/usage via `sacct`, so no worker route, no smoke job, no usage record.
  Accounting, QOS, GRES and controller containers were not modified.
- No host telemetry bindings; CPU/memory are scheduler registrations.
- Console header "GPU 등록 노드" counts the four virtual-slot nodes as GPU-registered nodes
  (registration count, unchanged frontend).
