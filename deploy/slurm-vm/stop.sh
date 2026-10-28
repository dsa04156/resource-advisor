#!/bin/bash
# Graceful ACPI shutdown of this VM only; the disk and keys are kept.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

pid=$(vm_pid) || { echo "not running"; exit 0; }
ssh -F "$SSH_CONFIG" "$SLURM_VM_NAME-admin" sudo systemctl poweroff 2>/dev/null \
  || python3 -I -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(b"system_powerdown\n")' "$MONITOR"
for _ in $(seq 120); do kill -0 "$pid" 2>/dev/null || { echo "stopped"; exit 0; }; sleep 1; done
echo "pid $pid still running; use: kill $pid" >&2; exit 1
