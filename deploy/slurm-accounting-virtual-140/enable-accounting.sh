#!/bin/bash
# enable-accounting.sh  (after up-accounting.sh; run as the docker-group operator)
# Registers cluster "sim" before the controller switch (accounting.html), backs up
# slurm.conf and StateSaveLocation, then restarts the existing slurmctld container in
# place only while its queue is empty. Nodes, GRES, QOS and partitions are unchanged.
#
# SlurmDBD always assigns a new random ClusterID (as_mysql_cluster_get_unique_id); the
# controller's existing ID is never sent, so its state-file ID cannot match. As the
# controller's own CLUSTER ID MISMATCH message documents, the one identity file
# StateSaveLocation/clustername is moved aside (kept) so the controller adopts the DBD
# ID. ClusterID feeds sluid/federation, not job IDs; job state and counters stay.
# If the controller does not answer within 60 s, slurm.conf and clustername are restored.
set -euo pipefail
CLUSTER_DIR=${CLUSTER_DIR:-$HOME/slurm-docker}
CONF=$CLUSTER_DIR/shared/slurm.conf
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=$CLUSTER_DIR/backup-ra-accounting-$STAMP
sacctmgr() { docker exec slurmdbd sacctmgr -i "$@"; }
if grep -q '^AccountingStorageType=' "$CONF"; then
  echo "slurm.conf already sets AccountingStorageType; not editing" >&2; exit 1
fi
docker exec slurmdbd sacctmgr -n -P show cluster format=cluster | grep -qx sim || sacctmgr add cluster sim
docker exec slurmdbd sacctmgr -n -P show account ra-lab format=account | grep -qx ra-lab \
  || sacctmgr add account ra-lab cluster=sim description=resource-advisor organization=lab
docker exec slurmdbd sacctmgr -n -P show assoc where cluster=sim account=ra-lab user=slurm format=user \
  | grep -qx slurm || sacctmgr add user slurm account=ra-lab cluster=sim
queue=$(docker exec --user slurm slurmctld squeue -a --noheader --format=%i)
[[ -z $queue ]] || { echo "queue not empty; refusing to restart slurmctld" >&2; exit 1; }
mkdir -m 700 "$BACKUP"
cp -p "$CONF" "$BACKUP/slurm.conf"
docker exec slurmctld tar -C /var/spool -cf - slurmctld >"$BACKUP/statesave.tar"
docker exec slurmctld scontrol show config >"$BACKUP/show-config.txt"
docker exec slurmctld cat /var/spool/slurmctld/clustername >"$BACKUP/clustername"
ASIDE=/var/spool/slurmctld/clustername.pre-ra-accounting-$STAMP
# Default TRES (cpu,mem,energy,node,billing,fs/disk,vmem,pages) are always tracked;
# gres/gpu adds accounting of the existing virtual slots, not a GRES definition change.
cat >>"$CONF" <<'CONF'
# Resource Advisor: accounting through the private slurmdbd container
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageHost=slurmdbd
AccountingStoragePort=6819
AccountingStorageTRES=gres/gpu
CONF
queue=$(docker exec --user slurm slurmctld squeue -a --noheader --format=%i)
if [[ -n $queue ]]; then
  cp -p "$BACKUP/slurm.conf" "$CONF"
  echo "queue changed during preparation; restored slurm.conf, no restart" >&2; exit 1
fi
up() {
  for _ in $(seq 30); do
    docker exec slurmctld scontrol ping >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}
docker exec slurmctld mv /var/spool/slurmctld/clustername "$ASIDE"
docker restart slurmctld >/dev/null
if ! up; then
  cp -p "$BACKUP/slurm.conf" "$CONF"
  up || { echo "controller not answering after restoring slurm.conf; backup: $BACKUP" >&2; exit 1; }
  # Without accounting the controller wrote a fresh ID; put the original identity back.
  docker exec slurmctld mv "$ASIDE" /var/spool/slurmctld/clustername
  docker restart slurmctld >/dev/null
  up
  echo "accounting switch failed; restored slurm.conf and clustername; backup: $BACKUP" >&2
  exit 1
fi
docker exec slurmctld scontrol reconfigure
echo "backup: $BACKUP"
