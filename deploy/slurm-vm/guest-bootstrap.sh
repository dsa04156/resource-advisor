#!/bin/bash
# Runs once inside the guest (cloud-init runcmd, as root). Configures a single-node
# Slurm lab: slurmctld + slurmd + slurmdbd backed by a local MariaDB.
# Usage: guest-bootstrap.sh <node-name> <cpus> <real-memory-mib>
set -euo pipefail
NODE=$1 CPUS=$2 REAL_MEMORY=$3
CLUSTER=ra-vm ACCOUNT=ra-project QOS=ra-normal USER_NAME=ra

install -d -o slurm -g slurm -m 755 /var/lib/slurm/slurmctld /var/lib/slurm/slurmd /var/log/slurm

# The DB password is generated here and never leaves the guest.
DB_PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
mariadb <<SQL
CREATE DATABASE IF NOT EXISTS slurm_acct_db;
CREATE USER IF NOT EXISTS 'slurm'@'localhost' IDENTIFIED BY '$DB_PASS';
ALTER USER 'slurm'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';
FLUSH PRIVILEGES;
SQL

install -o slurm -g slurm -m 600 /dev/stdin /etc/slurm/slurmdbd.conf <<EOF
AuthType=auth/munge
DbdHost=localhost
SlurmUser=slurm
LogFile=/var/log/slurm/slurmdbd.log
StorageType=accounting_storage/mysql
StorageHost=localhost
StorageUser=slurm
StoragePass=$DB_PASS
StorageLoc=slurm_acct_db
EOF

install -m 644 /dev/stdin /etc/slurm/slurm.conf <<EOF
ClusterName=$CLUSTER
SlurmctldHost=$NODE(127.0.0.1)
SlurmUser=slurm
AuthType=auth/munge
StateSaveLocation=/var/lib/slurm/slurmctld
SlurmdSpoolDir=/var/lib/slurm/slurmd
SlurmctldLogFile=/var/log/slurm/slurmctld.log
SlurmdLogFile=/var/log/slurm/slurmd.log
ProctrackType=proctrack/cgroup
TaskPlugin=task/cgroup,task/affinity
SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
JobAcctGatherType=jobacct_gather/cgroup
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageHost=localhost
AccountingStorageEnforce=associations,limits,qos
ReturnToService=2
# Private per-job /tmp and /dev/shm mount namespaces.
JobContainerType=job_container/tmpfs
PrologFlags=Contain
NodeName=$NODE NodeAddr=127.0.0.1 CPUs=$CPUS RealMemory=$REAL_MEMORY State=UNKNOWN
PartitionName=compute Nodes=$NODE Default=YES MaxTime=02:00:00 DefMemPerCPU=512 State=UP
EOF

install -m 644 /dev/stdin /etc/slurm/cgroup.conf <<EOF
CgroupPlugin=autodetect
ConstrainCores=yes
ConstrainRAMSpace=yes
EOF

install -m 644 /dev/stdin /etc/slurm/job_container.conf <<EOF
AutoBasePath=true
BasePath=/var/lib/slurm/job_container
Dirs=/tmp,/dev/shm
EOF

systemctl enable --now munge mariadb
systemctl enable slurmdbd slurmctld slurmd
systemctl restart slurmdbd
for _ in $(seq 60); do sacctmgr -n list cluster >/dev/null 2>&1 && break; sleep 2; done

sacctmgr -i add cluster "$CLUSTER" || true
sacctmgr -i add qos "$QOS" MaxWall=01:00:00 MaxTRESPerJob=cpu=$CPUS || true
sacctmgr -i add account "$ACCOUNT" Cluster="$CLUSTER" Description="Resource Advisor lab" Organization=ra || true
sacctmgr -i add user "$USER_NAME" Account="$ACCOUNT" Cluster="$CLUSTER" || true
sacctmgr -i modify user "$USER_NAME" set QOS="$QOS" DefaultQOS="$QOS"

systemctl restart slurmctld slurmd
for _ in $(seq 60); do sinfo -h -N -o '%T' | grep -qx idle && break; sleep 2; done
scontrol update NodeName="$NODE" State=RESUME 2>/dev/null || true

install -d -o "$USER_NAME" -g "$USER_NAME" -m 700 /srv/ra/work
# Root-owned, job-readable locations for qualified runtime files and training inputs.
install -d -o root -g root -m 755 /opt/ra /srv/ra/inputs /etc/ra
touch /var/lib/ra-slurm-bootstrap.done
