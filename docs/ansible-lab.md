# Actual ARM lab telemetry provisioning

This records the initial loopback qualification. The lab subsequently adopted
the explicitly configured [central mTLS transport](central-telemetry.md); the
loopback measurements below remain historical evidence.

The [frozen acceptance plan](ansible-lab-plan.md) was exercised on two authorized
standalone ARM lab workers. The corrected play installed and verified the pinned
node-exporter, preserved existing Slurm/driver identities, and passed a repeat
apply with zero changes. This closes the scoped host-telemetry provisioning
increment, not the complete Slurm automation or observability gates.

The implementation is [automation/ansible](../automation/ansible/README.md).
[Machine-readable observations](evidence/ansible-lab.json) retain the actual
measurements, play exit codes, recaps, binary hash and limitations. Node labels
below are publication aliases; raw logs, addresses, boot identities and private
inventory remain outside the public repository.

Implementation revision `81b290432a379bb2d675eb1b613316279aec100c` passed
[GitHub Actions](https://github.com/dsa04156/resource-advisor/actions/runs/37083116112):
the isolated hash-locked Ansible syntax job and both Python 3.11/3.13 contract jobs.
CI validates software; the separate host observations below establish the scoped
installation result.

## Actual observations

| Observation | ARM NPU lab host | ARM GPU lab host |
|---|---|---|
| Architecture | aarch64 | aarch64 |
| Python | 3.13.5 | 3.12.3 |
| Installed Slurm | 24.11.5 | 24.11.5 |
| node-exporter | 1.10.2 | 1.10.2 |
| CPU count from real counters | 4 | 6 |
| Successful enabled collectors | 8/8 | 8/8 |
| Independent scrape interval | 2.005929 s | 2.098369 s |
| Total CPU counter increase | 8.01 s | 12.56 s |
| Available memory at second scrape | 3,527,622,656 B | 6,638,112,768 B |
| Total memory reported by kernel | 4,241,620,992 B | 7,849,181,184 B |
| Repeat apply | 0 changed, 0 failed | 0 changed, 0 failed |
| Slurmd PID, activity and boot ID | Preserved | Preserved |

CPU counter increases sum all modes, including idle, across cores. They prove
fresh counter observations; they are not CPU utilization percentages or workload
performance measurements. Memory is the host kernel's observation, not a GPU/NPU
memory measurement or a reservation guarantee. The host aliases do not confer
accelerator qualification.

The installed binaries matched the pinned SHA-256 on both hosts. Each service
was enabled and active, had no automatic restarts, and retained the same PID
through the repeated corrected play. Independent listener and systemd queries
confirmed loopback-only port 19100, dynamic user, empty capabilities, 128 MiB
memory limit, 10% of one CPU core and 64-task limit. No reboot was performed, so
enabled service state is not a tested reboot-recovery claim.

The original existence and SHA-256 of `slurm.conf`, `gres.conf`, `cgroup.conf`,
Jetson release identity and NVIDIA driver version information matched before and
after. Absent files stayed absent. These checks cover those exact files, not an
assertion that every byte of the operating system was audited.

## Failures found and corrected

The first preflight changed zero targets but exposed an SSH/Ansible error path
when `systemctl is-active` returned its expected nonzero inactive-service code.
The guard now reads `systemctl show ... ActiveState --value`, then explicitly
rejects active Kubernetes/EdgeCore services. The corrected preflight and final
check-mode run succeeded with zero changes on both hosts.

The first installation returned zero and exposed CPU/memory metrics, but the
independent collector check found `netdev=0` on both hosts. The journal reported
an unsupported socket address family. The pinned exporter's network collector
[uses rtnetlink by default](https://github.com/prometheus/node_exporter/blob/v1.10.2/collector/netdev_linux.go).
Adding AF_NETLINK to the dedicated unit's address-family allowlist fixed that
specific failure. Only the exporter unit and its process changed during this
correction; Slurm process/configuration and host boot identity were preserved.
Apply now requires all eight enabled collectors to report success, in addition
to actual CPU/memory/network metric families. The independent scrapes then
passed for every collector. The initial failed observations were retained.

Two deliberate rejection trials also passed: an unapproved inventory stopped at
the first assertion, and a corrupted local binary stopped at its hash assertion.
Both returned exit 2 with zero changes. Serial fail-fast stopped on the first
host; these are not claims that every host was contacted by those negative runs.

## Remaining boundaries

This installation provides real host system telemetry through authenticated SSH
access to a loopback exporter. Central Prometheus ingestion and use by the
Resource Advisor console remain unverified. It supplies no GPU/NPU utilization.

Slurm discovery is read-only. Active worker daemons do not establish controller
reachability, scheduler admission, QOS enforcement or a model execution path.
Slurm configuration deployment, accelerator qualification, AMD64 provisioning,
production-node management and reboot/HA recovery remain outside this result.
