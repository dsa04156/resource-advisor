# Slurm host observations in the research console

Two standalone ARM lab hosts now publish actual CPU and memory observations
through mTLS Prometheus ingestion, an independent inventory collector, persistent
PostgreSQL, the authenticated project API and the existing console. The collector
does not use Kubernetes inventory to invent Slurm state.

**The Slurm controller remains explicitly unconfigured.** Its latest connection
probe did not succeed. The screen therefore shows measured host bars alongside
unknown scheduler reservations, accelerator registration and queue state. This
increment does not qualify physical Slurm submission or NPU execution.

The [acceptance plan](slurm-inventory-plan.md) preceded implementation.
[Raw, sanitized evidence](evidence/slurm-inventory.json) includes two actual
snapshots and their source times. The
[deployment runbook](../deploy/slurm-inventory/README.md) describes configuration
and the future controller qualification gate.

## What is implemented

- A separate `SlurmInventoryCollector` shares only the Prometheus reader and
  immutable observation store with the Kubernetes collector. It never submits
  work, calls kubectl, grants capabilities or changes scheduler configuration.
- Explicit local/SSH adapters invoke scoped `scontrol` and `squeue` commands with
  the Slurm v0.0.42 parser. Account/partition mismatches, duplicate identities,
  warnings, unsupported schemas, invalid allocations and missing observations
  cannot turn into free resources or an empty queue. These paths currently have
  source-schema software tests, not live controller acceptance.
- Host CPU usage and memory availability use independently observed host totals.
  Scheduler reservation subtraction remains a separate value. A healthy exporter
  does not produce a Slurm Ready badge or a claim that no accelerator exists.
- A separate queue panel counts scoped scheduler records. It explicitly says that
  array tasks are not expanded. The current live panel shows the unconfigured
  connection, not zero pending jobs.
- A fourth optional static Argo CD Application runs the collector without a
  Kubernetes token or execution role. The original four-image configuration still
  renders the original three Applications. No project permission was expanded.

## Actual verification

Implementation commit `8397fc015a7d9fbf58d23dcb1b015ee5808d129e` passed
[CI run 37088065208](https://github.com/dsa04156/resource-advisor/actions/runs/37088065208):
both Python 3.11 and 3.13 passed 569 SQLite tests with two PostgreSQL-only skips,
then 571 PostgreSQL tests. Lint, formatting, JavaScript parsing, wheel assets and
the independent Ansible syntax check also passed.

| Boundary | Observed result |
|---|---|
| Real telemetry | Four host metrics on each of two nodes; all eight source times advance between the retained API snapshots |
| Persistence | Distinct saved snapshots at 02:03:41 and 02:05:11 UTC on 2026-10-03 |
| Access | Certificate-verified HTTPS; anonymous access 401; other project 404 and no Slurm entry in its overview |
| Console | Four host meters; controller/accelerator/queue unknown; 390 px viewport without page overflow |
| Browser faults | One host unavailable retains the peer; stale values and local expiry hide meters; untrusted labels remain inert |
| Deployed source | API and collector source hashes match the qualified image build |
| GitOps | Four core Applications Synced/Healthy with Succeeded operations at the exact implementation commit |
| Preserved runtime | Existing inventory, worker and database Pod/container identities, nine static object UIDs, existing Secrets/PVC, compute ownership and non-inventory database fingerprints unchanged |

The API image changed and the new collector was added. The existing Kubernetes
inventory, worker and PostgreSQL images stayed unchanged. The separate telemetry
ScrapeConfig Application retained its earlier qualified revision. Manual sync and
`prune: false` were preserved. The build reused the qualified locked dependencies;
it did not upgrade Kubernetes, KubeEdge, Slurm or drivers.

Browser checks used the deployed assets through a private read-only localhost
proxy to the trusted HTTPS origin. Project authorization was separately verified
against the real API. Browser outage/staleness cases were response overrides,
not physical host outages. The previous
[central telemetry trial](central-telemetry.md) contains the actual exporter
stop/recovery experiment.

## Failures retained and limits

The first access after API Pod replacement reached the obsolete port-forward
target and failed TLS. The existing supervisor reconnected in three seconds;
no manual restart was issued. This is a lab tunnel, not highly available ingress.

The first three snapshots withheld the GPU host's CPU rate because its combined
health query failed at collection time. The other seven observations remained
valid. Later snapshots recovered all eight without weakening the health guard.
Historical queries returned healthy subconditions; they do not prove which
condition failed at the earlier realtime query. These partial snapshots remain
stored and are not presented as fully healthy samples.

The initial browser harness moved to the next response override before rendering
completed. Waiting for network completion and rendering fixed that test race;
the final local and deployed checks each passed all nine assertions. Screenshots
also verified both desktop and narrow layouts; private cluster screens were not
published.

Live controller schema/queue/resource validation, end-to-end Slurm model execution,
accelerator telemetry and NPU qualification remain open. These two hosts currently
provide host CPU/memory observations, not full static hardware discovery. Credential
renewal, inventory retention, production ingress and HA require separate acceptance.
The [full goal audit](goal-audit.md) remains incomplete.
