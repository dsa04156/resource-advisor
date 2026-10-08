# Dedicated controller observation identity

The [observer play](../../automation/ansible/slurm-observer.yml) provisions a separate
locked-password `ra-observer` account and one restricted SSH public key on an
explicitly authorized lab controller. It does not change sshd configuration,
restart services, create a Slurm association or grant submit/cancel privileges.
An existing unmanaged account/directory is rejected before writes.

The key uses OpenSSH `restrict` and a root-owned forced command. No interactive
shell, PTY, forwarding or user-chosen program is exposed. The
[standard-library handler](../../examples/slurm_observer.py) accepts only these two
argument vectors for the fixed account/partition in its root-owned scope:

```text
scontrol --json=v0.0.42 show nodes
squeue --json=v0.0.42 --account=<configured-account> --partition=<configured-partition>
```

It invokes absolute executable paths with a fixed environment and no shell. The
response must use the qualified 24.11.5 schema. Only explicitly allowed node names
and required resource fields are returned; addresses, arbitrary node annotations,
job names and commands are removed. Queue responses outside the account/partition
are rejected. Transport/schema failures are not empty queue observations.

Use a separate private inventory group `ra_slurm_observers` with these variables:

```yaml
ra_lab_authorized: true
ra_existing_production: false
ra_slurm_observer_authorized: true
ra_observer_public_key: "ssh-ed25519 <reviewed-public-key>"
ra_observer_scope:
  account: team-a
  partition: gpu
  nodes: [gpu-worker]
```

Generate the private key outside the repository. Pin the controller host key using
an already trusted channel, and test with strict host-key verification. Supply no
operator password or general-purpose SSH key to the collector. Public authorized
keys are root-owned/readable so sshd can validate them under the target UID; the
account cannot edit its own authorization or observer program.

Run syntax/check mode, a denied-authorization preflight, apply, positive node/queue
reads and negative submit/cancel/shell/foreign-scope commands. Repeat apply must
change zero tasks. Compare controller configuration, Slurm associations/QOS and
daemon process identities before and after. Check mode validates prerequisites;
it does not create the account or prove SSH login.

This transport needs a qualified SSH client and separately mounted private key
and known-hosts on the collector. **Provisioning the controller alone does not
enable live console data.** Do not bypass host verification or install private
credentials in an image layer.

## Collector image and credential mount

The original service image has neither OpenSSH nor a passwd entry for UID 10001.
For that qualified Debian 13 / Python 3.11 / amd64 base, the
[payload preparer](../../examples/prepare_ssh_layer.py) resolves a pinned OpenSSH
client version using signed apt indexes in a disposable container. It downloads
dependency archives without installing host packages, records archive hashes,
extracts their payloads, strips setuid/setgid bits and adds a non-login passwd
entry with home `/tmp`. It needs no Docker socket, credentials or host mounts.

Run the preparer only inside the exact base image, with a fresh output directory:

```sh
RA_SSH_IMAGE_PREP=disposable-container python prepare_ssh_layer.py \
  --output /tmp/ssh-bundle \
  --version 1:10.0p1-7+deb13u4 \
  --base-digest sha256:<exact-base-digest>
```

The guard checks the disposable-container opt-in, OS, architecture and UID. The
operator must independently verify the container's actual image digest; a CLI
argument cannot attest its own base. Dependency resolution uses the current
signed index, so retain the generated package versions, hashes and payload for
reproduction. This payload is **not dpkg-installed**: use its manifest alongside
the base image's package inventory when auditing dependencies. No package
maintainer scripts or SSH server are installed.

Append current application source with `update_service.py` using the qualified
base build report and unchanged `uv.lock`, then append `ssh-layer.tar` with
`crane mutate --append`. Retain both source and SSH provenance, pin the final
image by digest, and test it before rollout. A source-only update report does
not qualify the added SSH payload.

The optional `ra-slurm-observer-ssh` Secret supplies `config`, `id_ed25519` and
`known_hosts` at `/tmp/.ssh`, read-only with mode 0440 and fsGroup 10001. Its
`config` must use the dedicated observer account, `IdentityFile
/tmp/.ssh/id_ed25519`, `UserKnownHostsFile /tmp/.ssh/known_hosts`,
`IdentitiesOnly yes`, `BatchMode yes`, `StrictHostKeyChecking yes` and
`ClearAllForwardings yes`. Generate it outside Git. Set `controller.transport`
to `ssh` and `controller.ssh_target` to that configuration's Host alias. Preserve
the existing Prometheus bindings when adding controller/node/GRES mappings.

Qualify a separate Pod with UID 10001, read-only root, no service-account token
and the same Secret mount. Check the client version, loaded collector source
hash, actual node/queue reads, retained host metrics, and rejection of shell,
submit and cancel commands. Only then update the inventory Argo Application's
revision and image digest, with pruning disabled. API, worker and database
rollouts are not required for this observation-only change. Verify two fresh
API snapshots, project isolation and the console; scheduler reservations remain
distinct from measured utilization and execution qualification.
