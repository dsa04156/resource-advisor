# virtual-slurm-140 execution route: configured, NOT_VALIDATED

Execution transport for the Docker Slurm cluster described in
[`../slurm-accounting-virtual-140`](../slurm-accounting-virtual-140/README.md). The route
is **configured but not validated**: no job has run through it and none can until an
operator enables a qualified native runtime.

| File | Role |
|---|---|
| [`executor.json`](executor.json) | gateway `controller` scope: `docker exec --user slurm slurmctld`, account `ra-lab`, partition `all`, QOS `normal`, node `gpu-big`, `gres gpu:a100` ×1 (virtual slot), ≤2 min, output `/jobs/ra-lab`; `native_bindings: []`, `training_bindings: []` |
| [`results.json`](results.json) | gateway `results` scope: bounded tail of `/jobs/ra-lab/<attempt>.log`, read on the host at `~/slurm-docker/jobs/ra-lab`, owner UID 64030 (`slurm`) |
| [`worker-route.example.json`](worker-route.example.json) | worker route `ra-lab`/`virtual-slurm-140`, `native_runtimes: {}` |
| [`worker.yaml`](worker.yaml) | `ra-virtual-slurm-140-worker`, **replicas 0** |

Gateway files and the two keys were installed on 2026-10-29 and checked with read-only
`squeue`/`sacct` and denials only; see
[`docs/evidence/slurm-execution-140-20261029.md`](../../docs/evidence/slurm-execution-140-20261029.md).

## Why nothing can execute

- Worker: `native_runtimes` is empty, so `SlurmBackend.validate` raises
  `Slurm requires a qualified native runtime binding` before any SSH call.
- Gateway: empty `native_bindings` rejects every `sbatch` script
  (`native runtime is not enabled`) before Docker is invoked.
- Both are covered by `tests/test_slurm_executor.py`
  (`test_committed_virtual_140_route_and_gateway_cannot_submit`). No live `sbatch`
  was sent, including to prove rejection.

A worker holding only this route cannot lease other work: every outbox claim is
filtered by complete `(project, cluster)` pairs (`store.job_route_filter`).

## Transport

[`examples/slurm_executor.py`](../../examples/slurm_executor.py) gained an optional
operator-set `container`: the already-scoped argv runs as
`/usr/bin/docker exec [--interactive] --user <user> --env TZ=UTC --env SLURM_TIME_FORMAT=… <container> /usr/bin/<cmd> …`.
Requests cannot choose the container, user, path or flags. Two keys, separate from
the read-only observer key, each `restrict,from="<ADVISOR_EGRESS_IP>"`, forced to:

```
/usr/bin/python3 -I /home/etri/.ra-virtual-slurm/slurm_executor.py --config /home/etri/.ra-virtual-slurm/executor.json   # ra-virtual-slurm-140-executor
/usr/bin/python3 -I /home/etri/.ra-virtual-slurm/slurm_executor.py --config /home/etri/.ra-virtual-slurm/results.json    # ra-virtual-slurm-140-results
```

Private inputs (outside Git): Secret `ra-virtual-slurm-140-executor-ssh` (`config` with
hosts `ra-virtual-slurm-140-executor`/`-results`, both private keys, pinned
`known_hosts`) and Secret `ra-virtual-slurm-140-worker-config` (`worker.json`).

## Boundaries that remain

- **Job UID:** jobs would run as `slurm`, the cluster's `SlurmUser` (Slurm-privileged).
  Only the gateway's `--account/--partition/--user/--name` argv limits protect other
  jobs. A dedicated unprivileged job UID on all seven containers is required before
  any real execution; it is not created here (node change).
- **Gateway owner:** files are owned by the existing `etri` account, which is in the
  `docker` group (root-equivalent on that host). This is a command boundary for one
  trusted account, not a root-owned isolation boundary.
- **GPU:** `gres/gpu` are virtual slots; no physical device, A100 performance, model or
  training qualification follows. The training guard is unchanged.
