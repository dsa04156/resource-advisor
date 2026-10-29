#!/bin/bash
# up-accounting.sh <mariadb-image@sha256:...>  (run as the docker-group operator on the Slurm host)
# Adds a private MariaDB + SlurmDBD next to the running "sim" cluster. Never touches the
# existing seven containers, publishes no ports, and refuses to replace existing objects.
set -euo pipefail
MARIADB_IMAGE=${1:?mariadb image pinned by digest required}
[[ $MARIADB_IMAGE == *@sha256:* ]] || { echo "pin the MariaDB image by digest" >&2; exit 1; }
CLUSTER_DIR=${CLUSTER_DIR:-$HOME/slurm-docker}
HERE=$(cd "$(dirname "$0")" && pwd)
SECRET=$HERE/secret
for name in slurmdbd slurmdbd-mariadb; do
  if docker container inspect "$name" >/dev/null 2>&1; then
    echo "container $name already exists; not replacing it" >&2; exit 1
  fi
done
docker network inspect slurmnet >/dev/null
umask 077
mkdir -p "$SECRET"; chmod 700 "$SECRET"
# Hex secrets, generated here and never printed. The 0700 directory is the host
# boundary; the files are 0644 so MariaDB's unprivileged entrypoint can read them.
for f in db_password db_root_password; do
  [[ -s $SECRET/$f ]] || openssl rand -hex 24 >"$SECRET/$f"
  chmod 644 "$SECRET/$f"
done
docker build -q -t slurm-sim-dbd:24.11.5 -f "$HERE/Dockerfile.slurmdbd" "$HERE" >/dev/null
docker volume inspect ra-slurmdbd-mariadb >/dev/null 2>&1 || docker volume create ra-slurmdbd-mariadb >/dev/null
docker run -d --name slurmdbd-mariadb --hostname slurmdbd-mariadb --network slurmnet \
  --restart unless-stopped -v ra-slurmdbd-mariadb:/var/lib/mysql \
  -v "$SECRET/db_password:/run/ra-secret/db_password:ro" \
  -v "$SECRET/db_root_password:/run/ra-secret/db_root_password:ro" \
  -e MARIADB_DATABASE=slurm_acct_db -e MARIADB_USER=slurm \
  -e MARIADB_PASSWORD_FILE=/run/ra-secret/db_password \
  -e MARIADB_ROOT_PASSWORD_FILE=/run/ra-secret/db_root_password \
  "$MARIADB_IMAGE" --innodb-buffer-pool-size=256M --innodb-lock-wait-timeout=900 \
  --innodb-log-file-size=64M >/dev/null
docker run -d --name slurmdbd --hostname slurmdbd --network slurmnet --restart unless-stopped \
  -v "$CLUSTER_DIR/shared:/shared:ro" \
  -v "$HERE/slurmdbd.conf.template:/etc/ra/slurmdbd.conf.template:ro" \
  -v "$SECRET/db_password:/run/ra-secret/db_password:ro" \
  slurm-sim-dbd:24.11.5 >/dev/null
for _ in $(seq 60); do
  docker exec slurmdbd sacctmgr -n -P show cluster >/dev/null 2>&1 && { echo "slurmdbd ready"; exit 0; }
  sleep 2
done
echo "slurmdbd did not become ready; see docker logs slurmdbd" >&2; exit 1
