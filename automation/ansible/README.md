# Scoped ARM lab telemetry

This play manages only its dedicated node-exporter binary, ownership marker and
systemd service on explicitly authorized standalone ARM lab hosts. The Slurm role
is **read-only discovery**. It does not provision a scheduler, change Slurm
configuration, install drivers, qualify accelerators, or enroll nodes in Kubernetes.
Active Kubernetes/EdgeCore hosts and production inventory are rejected.

## Operator environment

Use a separate Python 3.11–3.13 environment; the application runtime does not need
Ansible. The pinned ansible-core 2.19 targets Python 3.8–3.13. Only aarch64/systemd
hosts and the exact binary below are accepted by this initial role.

```sh
uv venv /secure/ra-ansible --python 3.12
uv pip install --python /secure/ra-ansible/bin/python --require-hashes \
  -r automation/ansible/requirements.txt
export ANSIBLE_CONFIG="$PWD/automation/ansible/ansible.cfg"
```

Copy `inventory.example.yml` outside the repository. Supply the approved hosts,
SSH identity, Python interpreter and absolute `ra_node_exporter_binary` path.
Set `ra_lab_authorized: true` and `ra_existing_production: false` only for the
reviewed lab inventory. Keep credentials in private inventory/Ansible Vault or
an SSH agent. Verify SSH host keys normally; host-key checking stays enabled.

## Verify the release before provisioning

Download `node_exporter-1.10.2.linux-arm64.tar.gz` and `sha256sums.txt` from the
[official v1.10.2 release](https://github.com/prometheus/node_exporter/releases/tag/v1.10.2)
into a new private directory. Match the archive against both the published
checksum file and this pinned checksum before extraction:

| File | SHA-256 |
|---|---|
| ARM64 release archive | `de69ec8341c8068b7c8e4cfe3eb85065d24d984a3b33007f575d307d13eb89a6` |
| Extracted `node_exporter` binary | `123faecd856ab0aa317b2916fdb2cf09089a487930beca2956c6d3bb13e49a03` |

Extract only `node_exporter-1.10.2.linux-arm64/node_exporter`. The role independently
checks the local binary hash before writing any target and checks its installed
bytes before starting the service. No OS package repositories are changed.

## Preflight, apply and verify

```sh
/secure/ra-ansible/bin/ansible-playbook -i /secure/ra-inventory.yml \
  automation/ansible/site.yml --syntax-check
/secure/ra-ansible/bin/ansible-playbook -i /secure/ra-inventory.yml \
  automation/ansible/site.yml --check
/secure/ra-ansible/bin/ansible-playbook -i /secure/ra-inventory.yml \
  automation/ansible/site.yml
```

Check mode verifies prerequisites and preservation only: it deliberately skips
installation, service startup and metric retrieval, and is not a complete install
diff. Actual apply requires privilege escalation on the approved hosts. Serial
execution stops on the first failed host. Failure does not automatically remove
partial installation; retain logs and inspect the failed task before retrying.

The dedicated `resource-advisor-node-exporter.service` listens on
`127.0.0.1:19100`, uses a dynamic unprivileged user, caps CPU at 10% of one core and
memory at 128 MiB, and enables CPU, memory, load, system, network and disk counters.
The address-family allowlist includes AF_NETLINK because the pinned exporter
[reads network counters through rtnetlink](https://github.com/prometheus/node_exporter/blob/v1.10.2/collector/netdev_linux.go).
Apply requires success from all eight enabled collectors, not just HTTP 200.
It does not expose GPU/NPU utilization. Retrieve `/metrics` locally on the host or
through an authenticated SSH tunnel. Central Prometheus ingestion requires a
separately reviewed transport/scrape configuration; this play does not provide it.

Repeat the same apply and require `changed=0`, unchanged exporter PID, real CPU
counters advancing between scrapes, and available memory between zero and total
memory. Original Slurm/driver file hashes and slurmd process state are compared
within every play; independently compare host boot IDs before and after as well.
An active slurmd does not establish controller reachability, admission, accelerator
readiness or accounting availability.

For a negative preflight, pass `-e '{"ra_lab_authorized":false}'` and require the
first assertion to fail without changes. A modified local binary must also fail
before target writes. Keep failed-trial logs with the successful evidence.

To retire this installation, first stop and disable only
`resource-advisor-node-exporter.service`. Inspect the managed marker and unit,
then remove only that unit and `/opt/resource-advisor/node-exporter`, followed by
`systemctl daemon-reload`. Never remove another exporter's files or Slurm units.

See the [acceptance plan](../../docs/ansible-lab-plan.md) for the evidence boundary.
The [actual two-host report](../../docs/ansible-lab.md) includes the initial
collector failure, correction, preserved configuration and zero-change rerun.
