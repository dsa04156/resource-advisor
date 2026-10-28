#!/bin/bash
# install-gateway.sh <slurm_executor.py> <config.json>
# Installs the forced-command SSH gateway root-owned in the guest and issues a separate
# gateway key for Resource Advisor. User ra's authorized keys move to a root-owned file,
# so job code running as ra cannot remove the forced command.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

GATEWAY=${1:?gateway script} CONFIG=${2:?gateway config json}
python3 -I -c 'import json,sys; json.load(open(sys.argv[1]))' "$CONFIG"
K=$SLURM_VM_STATE/keys
[[ -f $K/ra_gateway_ed25519 ]] || (umask 077 && ssh-keygen -q -t ed25519 -N '' -C gateway@$SLURM_VM_NAME -f "$K/ra_gateway_ed25519")
FORCED="restrict,command=\"/usr/bin/python3 -I /opt/ra/slurm_executor.py --config /etc/ra/slurm-executor.json\" $(cat "$K/ra_gateway_ed25519.pub")"

admin() { ssh -F "$SSH_CONFIG" "$SLURM_VM_NAME-admin" "$@"; }
admin "sudo install -o root -g root -m 755 /dev/stdin /opt/ra/slurm_executor.py" <"$GATEWAY"
admin "sudo install -o root -g root -m 644 /dev/stdin /etc/ra/slurm-executor.json" <"$CONFIG"
# The operator shell key for ra stays; the gateway key may only run the forced command.
printf '%s\n%s\n' "$(cat "$K/ra_ed25519.pub")" "$FORCED" | admin \
  "sudo install -d -o root -g root -m 755 /etc/ra/ssh && sudo install -o root -g root -m 644 /dev/stdin /etc/ra/ssh/ra.authorized_keys"
printf 'Match User ra\n  AuthorizedKeysFile /etc/ra/ssh/ra.authorized_keys\n' | admin \
  "sudo install -m 644 /dev/stdin /etc/ssh/sshd_config.d/60-ra-gateway.conf && sudo sshd -t && sudo systemctl reload ssh"

grep -q "^Host $SLURM_VM_NAME-gateway\$" "$SSH_CONFIG" || cat >>"$SSH_CONFIG" <<EOF
Host $SLURM_VM_NAME-gateway
  HostName $SLURM_VM_BIND
  Port $SLURM_VM_SSH_PORT
  User ra
  IdentityFile $K/ra_gateway_ed25519
  UserKnownHostsFile $K/known_hosts
  StrictHostKeyChecking yes
  HostKeyAlgorithms ssh-ed25519
  IdentitiesOnly yes
  BatchMode yes
  ConnectTimeout 15
EOF
echo "gateway installed; key=$K/ra_gateway_ed25519 ssh alias=$SLURM_VM_NAME-gateway"
