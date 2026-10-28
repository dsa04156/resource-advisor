# Shared settings for the dedicated Slurm lab VM. Sourced by the other scripts.
set -euo pipefail

SLURM_VM_NAME=${SLURM_VM_NAME:-ra-slurm-vm}
SLURM_VM_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SLURM_VM_STATE=${SLURM_VM_STATE:-$(cd "$SLURM_VM_DIR/../../.." && pwd)/.state/slurm-vm-live}
SLURM_VM_BIND=${SLURM_VM_BIND:-127.0.0.1}
SLURM_VM_SSH_PORT=${SLURM_VM_SSH_PORT:-2232}
SLURM_VM_CPUS=${SLURM_VM_CPUS:-2}
SLURM_VM_MEMORY_MIB=${SLURM_VM_MEMORY_MIB:-4096}
SLURM_VM_DISK=${SLURM_VM_DISK:-16G}
# Slurm RealMemory must not exceed what the guest reports after kernel reservations.
SLURM_VM_REAL_MEMORY=${SLURM_VM_REAL_MEMORY:-3072}

DEBIAN_IMAGE_BASE=https://cloud.debian.org/images/cloud/trixie/latest
DEBIAN_IMAGE=debian-13-genericcloud-amd64.qcow2
QEMU_DEBS="qemu-system-x86 qemu-system-common qemu-system-data qemu-utils seabios ipxe-qemu genisoimage
libpmem1 librdmacm1t64 liburing2 libaio1t64 libibverbs1 libndctl6 libdaxctl1"

QEMU_ROOT=$SLURM_VM_STATE/qemu
# The extracted libraries are visible only to the private QEMU binaries.
qemu_env() {
  LD_LIBRARY_PATH=$QEMU_ROOT/usr/lib/x86_64-linux-gnu QEMU_MODULE_DIR=$QEMU_ROOT/usr/lib/x86_64-linux-gnu/qemu "$@"
}
QEMU=$QEMU_ROOT/usr/bin/qemu-system-x86_64
QEMU_IMG=$QEMU_ROOT/usr/bin/qemu-img
GENISOIMAGE=$QEMU_ROOT/usr/bin/genisoimage

DISK=$SLURM_VM_STATE/disk/$SLURM_VM_NAME.qcow2
SEED=$SLURM_VM_STATE/seed/seed.iso
PIDFILE=$SLURM_VM_STATE/run/qemu.pid
MONITOR=$SLURM_VM_STATE/run/monitor.sock
CONSOLE_LOG=$SLURM_VM_STATE/logs/console.log
SSH_CONFIG=$SLURM_VM_STATE/ssh_config

vm_pid() {
  [[ -s $PIDFILE ]] || return 1
  local pid
  pid=$(<"$PIDFILE")
  # Only trust the pidfile while it still names this VM's QEMU process.
  [[ -r /proc/$pid/cmdline ]] && tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q -- "$DISK" && echo "$pid"
}
