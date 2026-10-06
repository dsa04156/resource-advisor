# Dedicated controller observation identity

The [observer play](../automation/ansible/slurm-observer.yml) provisions a separate
locked-password `ra-observer` account and one restricted SSH public key on an
explicitly authorized lab controller. It does not change sshd configuration,
restart services, create a Slurm association or grant submit/cancel privileges.
An existing unmanaged account/directory is rejected before writes.

The key uses OpenSSH `restrict` and a root-owned forced command. No interactive
shell, PTY, forwarding or user-chosen program is exposed. The
[standard-library handler](../examples/slurm_observer.py) accepts only these two
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
and known-hosts on the collector. The current base inventory image has no SSH
client; **provisioning the controller alone does not enable live console data**.
Do not bypass host verification or install private credentials in an image layer.
