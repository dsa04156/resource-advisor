#!/bin/bash
# ssh.sh [ra|admin] [command...] — SSH with the pinned host key.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

target=$SLURM_VM_NAME
[[ ${1:-} == admin ]] && target=$SLURM_VM_NAME-admin
[[ ${1:-} == admin || ${1:-} == ra ]] && shift
exec ssh -F "$SSH_CONFIG" "$target" "$@"
