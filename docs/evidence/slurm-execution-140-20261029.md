# virtual-slurm-140 accounting + execution transport — 2026-10-29

Levels: **accounting deployed and queried live**; executor transport code + mock contract
tests passed and **installed live with read-only checks**; execution route **configured,
NOT_VALIDATED** (no job ran). Not physical GPU
evidence, not execution or training qualification. Zero native workloads were submitted.

## Accounting (live, Docker cluster `sim` on 10.254.192.140)

| Item | Result |
|---|---|
| Services | `slurmdbd` (`slurm-sim-dbd:24.11.5`, image `sha256:d06e2c7f…`, slurmdbd `24.11.5-4+deb13u1`) and `slurmdbd-mariadb` (`mariadb@sha256:c4124d1a…`, 11.8.9) on `slurmnet`, no published ports, named volume `ra-slurmdbd-mariadb` |
| Secrets | generated on the host into a 0700 directory, never printed; 0 matches of either password in `docker logs` or `docker inspect` env |
| Controller | `AccountingStorageType=accounting_storage/slurmdbd`, host `slurmdbd:6819`; TRES `cpu,mem,energy,node,billing,fs/disk,vmem,pages,gres/gpu,gres/gpumem,gres/gpuutil`; same container (not recreated), 6 nodes idle, `NEXT_JOB_ID=3` before and after |
| Registration | `sacctmgr show cluster`: `sim` at `172.19.0.2:6817`, RPC 10752; associations `sim/ra-lab/slurm` (QOS `normal`) |
| Query | `docker exec --user slurm slurmctld sacct -X …` exit 0, 0 rows (no jobs since accounting began) |
| Persistence | after `docker restart slurmdbd-mariadb slurmdbd`, associations still present and controller `sacct` exit 0; `sdiag` DBD agent queue 0 |
| Backups | `~/slurm-docker/backup-ra-accounting-20261029T073141Z/` and `…T073508Z/` (slurm.conf, `scontrol show config`, StateSaveLocation tar, `clustername`); original `clustername` (`sim|623`) also kept in the spool as `clustername.pre-ra-accounting-20261029T073508Z` |

### Incident during the switch (recorded as it happened)

1. 07:31:41Z first switch: after `docker restart slurmctld`, the controller crash-looped
   for about 90 s with `fatal: CLUSTER ID MISMATCH` (state file ID 623, DBD ID 927).
   The queue was empty; nodes kept running. `slurm.conf` was restored from the backup and
   the controller came back with 6 idle nodes and `NEXT_JOB_ID=3`, accounting off.
2. Cause (24.11.5 source): SlurmDBD assigns a random ClusterID
   (`as_mysql_cluster_get_unique_id`), and controller registration does not send its own,
   so pre-adding the cluster (as the accounting guide orders) cannot match an existing
   controller state.
3. 07:35:08Z, coordinator-approved: the identity file was moved aside (not deleted), the
   four accounting lines re-applied, the same container restarted; it adopted ID 927.
   `scontrol reconfigure` restarted it once more in place. Total: one ~90 s outage and
   two short in-place restarts, all with an empty queue.

## Execution transport

- `examples/slurm_executor.py`: optional `container` runs the already-scoped argv via
  `docker exec --user <user> <container>`; `host_output_dir` + `result_owner_uid` for
  host-side result reads. Requests cannot choose the container, user, path or flags.
- Route `ra-lab`/`virtual-slurm-140`: gateway scope `gpu-big`, `gpu:a100` ×1
  (virtual slot), empty `native_bindings`/`training_bindings`; worker `native_runtimes: {}`;
  Deployment `ra-virtual-slurm-140-worker` at 0 replicas.
- Tests: `tests/test_slurm_executor.py` 10 passed (5 new: container wrapping, invalid
  container/user, submission rejected before Docker without an enabled runtime, host
  result mapping, configured result owner, committed route/gateway cannot submit);
  20 related gateway tests in `test_slurm_training.py`/`test_integrated_operations.py` passed.
  `tests/test_slurm_accounting_script.py` (stubbed Docker): 2 regressions for the two review
  P2s in the rollback path (fixed in 49ef4bc, fde5d54; each fails on the prior commit). The
  live switch had already run with the earlier script; the fixed script was copied to the host
  (sha256 matching the final commit) and not re-run; a failed `clustername` copy is reported, not claimed restored.
- Review: independent spec + standards reviews of 16811e1...7caa130 found no transport
  defects; the two rollback P2s above are fixed.
- Installed 07:43Z: `~/.ra-virtual-slurm/slurm_executor.py` (sha256 `c4169551…`, 0600),
  `executor.json`, `results.json`; `authorized_keys` backed up, two lines added
  (`ra-virtual-slurm-140-executor`, `-results`, both `restrict,from="10.254.192.47"`), observer
  line unchanged; `/jobs/ra-lab` created as `slurm` (empty).
- Live checks from 10.254.192.47 (no `sbatch`, no port forwarding tested):

| Key | Request | Result |
|---|---|---|
| executor | `squeue` 5-column and 7-column ra-lab/all forms | exit 0, 0 rows |
| executor | `sacct` by name (`--starttime`) and by `--jobs 1 --duplicates` | exit 0, 0 rows |
| executor | `id`, `sinfo`, `docker ps`, `bash -i`, no command, chained `…; id`, foreign-account `squeue`/`sacct`, `tail` | rejected (exit 1) |
| results | `tail -c 65537 -- /jobs/ra-lab/none.log` | rejected: no such owned file (positive read **not validated**; no job output exists) |
| results | `id`, `/etc/passwd`, `/jobs/inf-1a-3.out` (outside scope), `squeue` | rejected (exit 1) |

## No new native jobs

| Counter | 07:29:31Z (initial) | 07:39:43Z (after accounting) | 07:43:50Z (final) |
|---|---|---|---|
| `squeue -a` rows | 0 | 0 | 0 |
| `NEXT_JOB_ID` | 3 | 3 | 3 |
| `~/slurm-docker/jobs` entries | 7 | 7 | 8 (new empty `ra-lab/` directory) |
| RA `ra_jobs` / `ra_outbox` / `ra_usage` total | 878 / 3918 / 876 | 878 / 3918 / 876 | 878 / 3918 / 876 |
| RA rows mentioning `virtual-slurm-140` (jobs, outbox, usage, non-inventory entities) | 0, 0, 0, 0 | 0, 0, 0, 0 | 0, 0, 0, 0 |
| Deployments `ra-api`, `ra-worker`, `ra-inventory`, `ra-virtual-slurm-140-inventory` | 1/1 each | 1/1 each | 1/1 each |
| `ra-slurm-worker`, `ra-slurm-inventory` | 0 | 0 | 0 |

Final `sacct --accounts=ra-lab`: 0 rows. `ra-virtual-slurm-140-worker` is not deployed
(manifest only, 0 replicas).

## Not done / limitations

- **Execution NOT_VALIDATED**: no qualified native runtime, no job UID separate from
  `SlurmUser` (`slurm`), no run. Before any real execution: a dedicated unprivileged job
  UID on all seven containers and a qualified runtime binding.
- Gateway is owned by the `etri` account (docker group, root-equivalent on the host): a
  command boundary for one trusted account, not a root-owned isolation boundary.
- Allocated `gres/gpu` time will be virtual-slot time, not GPU utilization. `gres/gpumem` and
  `gres/gpuutil` were registered automatically by Slurm; with `JobAcctGatherType` unset they are
  not measured, so their values are unknown.
- Pre-existing, unchanged: `shared/munge.key` is mode 0644 on the host; `client/slurm.conf`
  (external client bundle) does not carry the accounting lines.
- `slurmdbd` logs a non-recommended `innodb_buffer_pool_size` (256M) — acceptable for this lab.
