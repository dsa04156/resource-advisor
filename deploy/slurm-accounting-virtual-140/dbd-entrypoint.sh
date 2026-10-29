#!/bin/bash
# Same munge key as the cluster (read-only /shared); DB password from a mounted file.
set -euo pipefail
cp /shared/munge.key /etc/munge/munge.key
chown munge:munge /etc/munge/munge.key /run/munge; chmod 400 /etc/munge/munge.key
su -s /bin/bash munge -c "munged --force"
pass=$(cat /run/ra-secret/db_password)
[[ $pass =~ ^[a-f0-9]{48}$ ]] || { echo "db_password must be 48 hex characters" >&2; exit 1; }
umask 077
sed "s/@STORAGE_PASS@/$pass/" /etc/ra/slurmdbd.conf.template >/etc/slurm/slurmdbd.conf
chown slurm:slurm /etc/slurm/slurmdbd.conf; chmod 600 /etc/slurm/slurmdbd.conf
unset pass
# Client config for sacctmgr/sacct inside this container only (not the cluster's slurm.conf).
printf '%s\n' ClusterName=sim SlurmctldHost=slurmctld AuthType=auth/munge \
  AccountingStorageType=accounting_storage/slurmdbd AccountingStorageHost=localhost \
  >/etc/slurm/slurm.conf
chmod 644 /etc/slurm/slurm.conf
for _ in $(seq 60); do (exec 3<>/dev/tcp/slurmdbd-mariadb/3306) 2>/dev/null && break; sleep 2; done
exec slurmdbd -D -vv
