#!/bin/bash
# Host process, guest identity and Slurm health.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

pid=$(vm_pid) || { echo "vm: not running"; exit 1; }
echo "vm: pid=$pid ssh=$SLURM_VM_BIND:$SLURM_VM_SSH_PORT"
ps -o pid,etime,rss,args -p "$pid" | cut -c1-200
ssh -F "$SSH_CONFIG" "$SLURM_VM_NAME-admin" '
  . /etc/os-release; echo "guest: $PRETTY_NAME kernel=$(uname -r) host=$(hostname)"
  test -f /var/lib/ra-slurm-bootstrap.done && echo bootstrap: done || echo bootstrap: pending
  systemctl is-active munge mariadb slurmdbd slurmctld slurmd | paste -sd" " | sed "s/^/services(munge mariadb slurmdbd slurmctld slurmd): /"
  sinfo -V; sinfo -N -o "%N %P %T %c %m"'
