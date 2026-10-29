# SlurmDBD accounting for the Docker "sim" cluster (`virtual-slurm-140`)

Adds persistent accounting to the user-supplied Docker Slurm 24.11.5 cluster on the
`10.254.192.140` host (`~/slurm-docker`, containers `slurmctld`, `cpu-1/2`, `gpu-big`,
`gpu-mid`, `gpu-s1/2` on network `slurmnet`). The host's native `etri-slurm`
(ports 6817/6819, MariaDB on 127.0.0.1:3306) is a different cluster and is not touched.

| Item | Value |
|---|---|
| `slurmdbd` | image `slurm-sim-dbd:24.11.5` from [`Dockerfile.slurmdbd`](Dockerfile.slurmdbd): `debian:trixie@sha256:2dc419c6…`, `slurmdbd`/`slurm-client` `24.11.5-4+deb13u1`, `munge` `0.5.16-1.1~deb13u1` (same build as `slurm-sim:24.11`) |
| Storage | `mariadb@sha256:c4124d1afa875e47f2892edf26b482fa54f796e2f342d3717a04ca091a22a9bd` (11.8.9), named volume `ra-slurmdbd-mariadb`, database `slurm_acct_db` |
| Network | both containers on `slurmnet` only; **no published ports** |
| Munge | the cluster's existing key, read from `shared/` (read-only bind) and copied into the container |
| Controller change | four lines appended to `shared/slurm.conf`: `AccountingStorageType=accounting_storage/slurmdbd`, `AccountingStorageHost=slurmdbd`, `AccountingStoragePort=6819`, `AccountingStorageTRES=gres/gpu` |
| Associations | cluster `sim`, account `ra-lab`, user `slurm` in `ra-lab` (default QOS `normal`; `AccountingStorageEnforce` stays `none`) |

`AccountingStorageTRES=gres/gpu` only records the existing virtual slots in
accounting (Slurm also adds `gres/gpumem`, `gres/gpuutil`). GRES definitions, counts,
nodes, partitions and QOS are unchanged. Allocated `gres/gpu` time is virtual-slot
time, never physical GPU utilization: the devices are `/dev/null` mapped as `/dev/nvidiaN`.

## Scripts (copied to `~/slurm-docker/accounting/`, run as the docker-group operator)

```sh
./up-accounting.sh mariadb@sha256:c4124d1a…   # build dbd image, start the two containers
./enable-accounting.sh                        # associations, backups, guarded controller switch
```

`up-accounting.sh` refuses to replace existing containers and generates the two
database passwords into `secret/` (directory 0700) without printing them;
`dbd-entrypoint.sh` renders `slurmdbd.conf` (0600, owner `slurm`) inside the container from
[`slurmdbd.conf.template`](slurmdbd.conf.template) (keys from SchedMD
`slurm-24-11-5-1` `etc/slurmdbd.conf.example`).

`enable-accounting.sh` stops unless the queue is empty (checked before and after
preparation), backs up `slurm.conf`, `scontrol show config`, the whole
`StateSaveLocation` (tar) and `clustername` into `~/slurm-docker/backup-ra-accounting-<UTC>/`,
then restarts the **same** `slurmctld` container (never recreated; `up.sh`/`down.sh` not used).

### Cluster ID

SlurmDBD always assigns a new random ClusterID (`as_mysql_cluster_get_unique_id`), and
the controller's ID is not sent on registration, so a controller with existing state
always mismatches (`fatal: CLUSTER ID MISMATCH`). The script therefore moves the one
identity file `StateSaveLocation/clustername` aside to
`clustername.pre-ra-accounting-<UTC>` (the override that message documents); the
controller adopts the DBD ID and rewrites the file. ClusterID feeds sluid/federation,
not job IDs; job state and `NEXT_JOB_ID` are kept. If the controller does not answer
within 60 s, it is stopped, `slurm.conf` and the original `clustername` are restored from
the backup with `docker cp` (no controller RPC needed), and the same container is started again.

## Rollback

With an empty queue: `cp backup-ra-accounting-<UTC>/slurm.conf shared/slurm.conf`,
`docker exec slurmctld mv /var/spool/slurmctld/clustername.pre-ra-accounting-<UTC>
/var/spool/slurmctld/clustername`, `docker restart slurmctld`. Then
`docker stop slurmdbd slurmdbd-mariadb` (the volume keeps the accounting history).
