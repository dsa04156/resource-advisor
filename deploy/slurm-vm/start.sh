#!/bin/bash
# Start the VM in the background with TCG (no KVM access needed). SSH is the only forwarded port.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if pid=$(vm_pid); then echo "already running pid=$pid"; exit 0; fi
[[ -f $DISK && -f $SEED ]] || { echo "run provision.sh first" >&2; exit 1; }
if [[ -r /dev/kvm && -w /dev/kvm ]]; then ACCEL=kvm; else ACCEL=tcg,thread=multi; fi

qemu_env "$QEMU" -name "$SLURM_VM_NAME",process="$SLURM_VM_NAME" \
  -machine q35 -accel "$ACCEL" -cpu max -smp "$SLURM_VM_CPUS" -m "$SLURM_VM_MEMORY_MIB" \
  -L "$QEMU_ROOT/usr/share/seabios" -L "$QEMU_ROOT/usr/share/qemu" \
  -drive if=virtio,file="$DISK",format=qcow2,discard=unmap \
  -drive if=virtio,file="$SEED",format=raw,readonly=on \
  -netdev user,id=n0,hostfwd=tcp:"$SLURM_VM_BIND":"$SLURM_VM_SSH_PORT"-:22 \
  -device virtio-net-pci,netdev=n0,romfile= \
  -device virtio-rng-pci \
  -display none -serial file:"$CONSOLE_LOG" \
  -monitor unix:"$MONITOR",server,nowait \
  -pidfile "$PIDFILE" -daemonize
echo "started pid=$(<"$PIDFILE") accel=$ACCEL ssh=$SLURM_VM_BIND:$SLURM_VM_SSH_PORT"
