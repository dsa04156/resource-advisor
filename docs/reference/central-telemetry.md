# Verified central host telemetry

Two existing ARM lab workers now supply real CPU/memory/system counters to the
existing Prometheus through certificate-verified HTTPS with mandatory client
authentication. The independent Resource Advisor metric reader consumes these
series with health and source-time checks. This qualifies the central telemetry
path. The subsequent [Slurm inventory integration](slurm-inventory.md) now
publishes these observations through the project API and console; controller
reservations, queues and accelerator state remain explicitly unconfigured.

The [acceptance plan](central-telemetry.md) preceded the trial.
[Observed results](../evidence/central-telemetry.json) contain actual values,
timestamps and scope. The [deployment runbook](../../deploy/telemetry/README.md)
describes the Ansible mode and separate GitOps application.

## Observed results

| Check | Actual result |
|---|---|
| Exporters | Two pinned node-exporter 1.10.2 ARM64 binaries |
| Host metric families | All eight selected collectors successful on both hosts |
| Client authentication | Missing and foreign certificates rejected on both hosts |
| Server authentication | Wrong server identity rejected on both hosts |
| Plaintext | HTTP returned 400; no metrics |
| Server private keys | Root-owned mode 0600 in mode 0700 directory; systemd credentials |
| Client private keys | Not installed in either worker's managed TLS directory |
| Prometheus | Existing 3.10.0 retained, both targets up=1 |
| Existing monitoring | All 27 preexisting scrape jobs/configuration and Pod/container identities preserved |
| Argo CD | One ScrapeConfig; Synced/Healthy, successful manual sync, no pruning |
| Original Slurm/driver state | Protected hashes, daemon identity and boot IDs preserved |
| Repeat apply | Both hosts changed=0, failed=0 |

Example real observations at 2026-10-03 01:14 UTC:

| Published alias | CPU count | CPU non-idle cores | Available memory | Total memory |
|---|---:|---:|---:|---:|
| slurm-npu-lab | 4 | 0.01333 | 3,674,439,680 B | 4,241,620,992 B |
| slurm-gpu-lab | 6 | 0.08778 | 6,676,623,360 B | 7,849,181,184 B |

These are point observations, not benchmark scores, scheduler headroom or proof
of GPU/NPU readiness. Non-idle cores count time outside the kernel's idle CPU
mode; they do not measure per-job utilization. Readiness/admission must come from
the scheduler and model/runtime qualification separately.

## Freshness and actual interruption

The exact [metric bindings](../../examples/slurm-host-bindings.json) carry explicit
health predicates and original source timestamps. They are metric-reader inputs,
not a complete Kubernetes collector configuration: do not register these Slurm
hosts as Kubernetes nodes to make the existing collector accept them.

CPU rates require at least four source samples in a one-minute window and a
healthy `up` window. Cold-start rates from the first few scrapes were excluded
from qualification. The timestamp expression takes the oldest metric, `up` and
collector-health sample; it labels timestamp *values* before combining them so
PromQL does not collide on identical label sets or substitute evaluation time.

Only the NPU-lab host's dedicated exporter was stopped. The first failed scrape
reported `up=0`; all four metric-reader observations became null/unknown while
the other host stayed healthy. The exporter was restored in a `finally` block,
and both hosts subsequently returned valid values, including a requalified CPU
window. Source timestamps advanced across independent observations. Detection
depends on the 15-second scrape interval and timeout; it is not instantaneous.

The initial fault harness expected the specific `collector_unhealthy` string.
Prometheus removed stale collector series after the failed scrape, so the
combined predicate was absent and the reader correctly returned `unavailable`
with null values. That assertion failed and its original observations/exit were
retained. A separate evidence audit verified the original plan's unknown-value
requirement and actual recovery; the outage was not repeated merely to obtain a
green harness exit.

Argo success preceded the actual Prometheus config reload. The trial waited for
the new targets and observed data rather than accepting sync status as telemetry
evidence. Existing Prometheus processes and configuration outside the one new
scrape job were independently compared and stayed unchanged.

## Software checks and remaining work

Implementation `753cf30df0f969a87e75740cb9588a9632c4c5fb` passed
[CI](https://github.com/dsa04156/resource-advisor/actions/runs/37084568804), including
the isolated Ansible syntax job and both Python 3.11/3.13 contract jobs. Local
SQLite tests passed 543 cases with two PostgreSQL-only skips; lint/format passed.
Real TLS, collection and outage evidence above is separate from software tests.

Certificates have a recorded expiration and require operator rotation. Automatic
renewal, production CA rotation, reboot recovery and HA are unqualified. Slurm
controller admission, GPU/NPU utilization and model execution are not established
by these host metrics. The full Notion objective remains open.
