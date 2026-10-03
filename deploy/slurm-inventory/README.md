# Optional Slurm inventory collector

This read-only collector writes project-scoped observations to Resource Advisor's
own database. It has no Kubernetes token, submit/cancel loop or execution role.
Provision the private `ra-slurm-inventory-config` Secret with key `inventory.json`,
and the existing `ra-service-db` Secret separately. Use the optional
`slurm-inventory` image in the [GitOps renderer](../argocd/README.md) and manually
sync only reviewed changes. Images must be qualified and pinned by digest.

The smallest configuration explicitly leaves the controller unconfigured:

```json
{
  "project_ref": "team-a",
  "cluster_ref": "lab-slurm",
  "node_refs": ["slurm-npu-lab", "slurm-gpu-lab"],
  "controller": null,
  "metrics": []
}
```

Add a private `prometheus_url` and the reviewed
[host metric bindings](../../examples/slurm-host-bindings.json) to observe actual
CPU and memory. Binding node references must belong to the configured pool. A
healthy exporter proves host telemetry only: reservations, accelerator
registration and the queue remain unknown while the controller is unconfigured.

To qualify scheduler observations later, configure a local Slurm client or an
SSH client with mounted private keys and known-hosts outside the public repository:

```json
{
  "transport": "ssh",
  "ssh_target": "observer@slurm.example.invalid",
  "account": "team-a",
  "partition": "gpu",
  "data_parser": "v0.0.42"
}
```

Set `node_names` to an explicit alias-to-scheduler-name mapping for every allowed
node. This route executes only `scontrol --json=v0.0.42 show nodes` and
`squeue --json=v0.0.42 --account=... --partition=...`. The base deployment does not
provision client binaries or SSH credentials: qualify these on a reviewed runtime
before configuring the route. Parser tests use Slurm 24.11.5 source-schema fixtures;
they are not evidence of a working physical controller or successful submission.
The parser contract comes from [SchedMD's v0.0.42 source](https://github.com/SchedMD/slurm/blob/slurm-24-11-5-1/src/plugins/data_parser/v0.0.42/parsers.c).

Classify counted `gres/...` keys explicitly through `gres_types`; no model or
accelerator execution qualification follows from a scheduler registration. Missing
allocation or drain fields stay unknown. CPU/memory reservation arithmetic differs
from measured host usage and does not imply admission. Queue summaries count
returned records, not expanded array tasks, and expose no job names or commands.
Warnings, scope mismatches, duplicate identities and unsupported schemas make the
affected source unavailable. Other sources continue independently. Source age is
checked when collected, when read through the API, and while displayed in the UI.
