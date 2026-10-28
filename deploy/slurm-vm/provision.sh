#!/bin/bash
# Prepare the dedicated Slurm VM without root: user-space QEMU from Ubuntu debs,
# a checksum-verified Debian 13 cloud image, private SSH keys and a cloud-init seed.
# Idempotent; never touches an existing disk (delete it by hand to re-provision).
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

umask 077
mkdir -p "$SLURM_VM_STATE"/{debs,qemu,image,keys,seed,logs,run,disk}
chmod 700 "$SLURM_VM_STATE"

if [[ ! -x $QEMU ]]; then
  (cd "$SLURM_VM_STATE/debs" && apt-get download $QEMU_DEBS)
  for deb in "$SLURM_VM_STATE"/debs/*.deb; do dpkg-deb -x "$deb" "$QEMU_ROOT"; done
fi
(cd "$SLURM_VM_STATE/debs" && sha256sum ./*.deb) >"$SLURM_VM_STATE/debs/SHA256SUMS"
if qemu_env ldd "$QEMU" | grep 'not found'; then echo "missing QEMU libraries" >&2; exit 1; fi

cd "$SLURM_VM_STATE/image"
if [[ ! -s $DEBIAN_IMAGE ]]; then
  curl -fsSLO "$DEBIAN_IMAGE_BASE/SHA512SUMS"
  curl -fsSLO "$DEBIAN_IMAGE_BASE/$DEBIAN_IMAGE"
fi
grep -E " $DEBIAN_IMAGE\$" SHA512SUMS | sha512sum -c -
chmod 444 "$DEBIAN_IMAGE"

K=$SLURM_VM_STATE/keys
[[ -f $K/ra_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -C ra@$SLURM_VM_NAME -f "$K/ra_ed25519"
[[ -f $K/admin_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -C admin@$SLURM_VM_NAME -f "$K/admin_ed25519"
# The guest host key is generated here so the client can pin it before first boot.
[[ -f $K/host_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -C $SLURM_VM_NAME -f "$K/host_ed25519"
echo "[$SLURM_VM_BIND]:$SLURM_VM_SSH_PORT $(cut -d' ' -f1-2 "$K/host_ed25519.pub")" >"$K/known_hosts"

cat >"$SSH_CONFIG" <<EOF
Host $SLURM_VM_NAME $SLURM_VM_NAME-admin
  HostName $SLURM_VM_BIND
  Port $SLURM_VM_SSH_PORT
  UserKnownHostsFile $K/known_hosts
  StrictHostKeyChecking yes
  HostKeyAlgorithms ssh-ed25519
  IdentitiesOnly yes
  BatchMode yes
  ConnectTimeout 15
Host $SLURM_VM_NAME
  User ra
  IdentityFile $K/ra_ed25519
Host $SLURM_VM_NAME-admin
  User raadmin
  IdentityFile $K/admin_ed25519
EOF

indent() { sed 's/^/      /' "$1"; }
cat >"$SLURM_VM_STATE/seed/user-data" <<EOF
#cloud-config
hostname: $SLURM_VM_NAME
manage_etc_hosts: true
ssh_pwauth: false
disable_root: true
ssh_deletekeys: true
ssh_genkeytypes: []
ssh_keys:
  ed25519_private: |
$(indent "$K/host_ed25519")
  ed25519_public: $(cat "$K/host_ed25519.pub")
users:
  - name: ra
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys: ["$(cat "$K/ra_ed25519.pub")"]
  - name: raadmin
    shell: /bin/bash
    lock_passwd: true
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys: ["$(cat "$K/admin_ed25519.pub")"]
package_update: true
packages: [munge, mariadb-server, slurmctld, slurmd, slurmdbd, slurm-client, python3, python3-numpy]
write_files:
  - path: /usr/local/sbin/ra-slurm-bootstrap
    permissions: "0755"
    encoding: b64
    content: $(base64 -w0 "$SLURM_VM_DIR/guest-bootstrap.sh")
runcmd:
  - [/usr/local/sbin/ra-slurm-bootstrap, $SLURM_VM_NAME, "$SLURM_VM_CPUS", "$SLURM_VM_REAL_MEMORY"]
EOF
printf 'instance-id: %s-1\nlocal-hostname: %s\n' "$SLURM_VM_NAME" "$SLURM_VM_NAME" >"$SLURM_VM_STATE/seed/meta-data"
qemu_env "$GENISOIMAGE" -quiet -output "$SEED" -volid cidata -joliet -rock \
  "$SLURM_VM_STATE/seed/user-data" "$SLURM_VM_STATE/seed/meta-data"

if [[ ! -f $DISK ]]; then
  qemu_env "$QEMU_IMG" create -q -f qcow2 -F qcow2 -b "$SLURM_VM_STATE/image/$DEBIAN_IMAGE" "$DISK" "$SLURM_VM_DISK"
fi

{ qemu_env "$QEMU" --version | head -1; qemu_env "$QEMU_IMG" --version | head -1
  dpkg-deb -f "$SLURM_VM_STATE"/debs/qemu-system-x86_*.deb Package Version
  grep -E " $DEBIAN_IMAGE\$" SHA512SUMS; } >"$SLURM_VM_STATE/versions.txt"
echo "provisioned $SLURM_VM_STATE"
