# Central host telemetry

This optional configuration connects the dedicated Ansible node-exporters to
the existing Prometheus Operator. It provisions no Prometheus server, scheduler,
GPU driver or runtime worker. Verify the installed ScrapeConfig CRD and the
Prometheus object's namespace/label selectors before using it. The lab selector
is `release: prometheus`; other installations need their own reviewed overlay.

## Transport

Keep the default loopback mode for host-only inspection. To opt an authorized
standalone host into central collection, set private Ansible inventory variables:

```yaml
ra_node_exporter_transport: mtls
ra_node_exporter_listen: "192.0.2.2:19100" # replace with that host's own IPv4 address
ra_node_exporter_tls_files:
  ca: /secure/telemetry/ca.crt
  server_cert: /secure/telemetry/host.crt
  server_key: /secure/telemetry/host.key
  client_cert: /secure/telemetry/prometheus.crt
  client_key: /secure/telemetry/prometheus.key
```

Use a CA dedicated to these telemetry endpoints, server certificates with the
target IP in their Subject Alternative Name and serverAuth usage, and a client
certificate with clientAuth usage. Use short expirations with an explicit
rotation date. Keep CA and client private keys off the workers. Ansible requires
systemd 247 or newer for LoadCredential; it installs only the server identity and
CA under a root-only directory. The dynamic exporter reads the credentials from
systemd's credential directory. The operator-side verification uses the client
certificate and verifies the server CA/identity. No insecure TLS bypass exists.

Run the existing syntax/preflight/apply instructions with this private inventory.
Only this dedicated exporter restarts when its unit or credentials change.
Existing Slurm/driver preservation checks still apply. A failed service or TLS
check leaves evidence and partial files for diagnosis; it is not an automatic
rollback to an unauthenticated listener.

## Prometheus and GitOps

Separately create `ra-node-telemetry-client` in `resource-advisor-lab`, with keys
`ca.crt`, `client.crt`, `client.key`. Never commit the Secret or put it in the Argo
source. Prometheus Operator consumes this Secret for verified HTTPS scrapes.
The namespace's credential readers must be trusted; this is not a public
telemetry endpoint or a multi-tenant credential isolation demonstration.

Create a private site JSON with a full pushed commit and approved targets:

```json
{
  "revision": "<full 40-character pushed Git SHA>",
  "targets": [{"node_ref": "slurm-gpu-lab", "address": "192.0.2.2"}]
}
```

Render outside the checkout:

```sh
uv run python deploy/telemetry/render.py \
  --site /secure/telemetry-site.json --output /secure/telemetry-argocd.json
```

Review and server-dry-run the rendered AppProject/Application, then create them
and manually sync `resource-advisor-telemetry` without pruning. The separate
project permits only namespaced ScrapeConfig in the lab namespace. The existing
three platform Applications are unaffected. Runtime Jobs and Secrets are never
managed by this source. Do not apply the base's documentation target directly.

Require the actual Argo render to match the private targets, then inspect real
Prometheus targets, `up`, collector success, raw sample timestamps and measured
CPU/memory values. A healthy exporter never establishes Slurm admission or model
qualification. Use `up` and per-collector success as health predicates and the
oldest contributing source timestamp when binding metrics into inventory.

## Rotation and retirement

Rotate server certificates by updating the private input files and reapplying;
the dedicated exporter restarts to reload systemd credentials. Rotate the client
certificate by updating the private Secret. For CA rotation, stage overlapping
old/new trust on both sides, rotate identities, verify scrapes, then remove old
trust. Validate the particular overlap procedure in the lab before production;
automatic renewal and production CA operations are not supplied here.

To retire collection, remove only this Application's ScrapeConfig explicitly,
then its dedicated client Secret once no consumers remain. Stop/retire the
dedicated exporters separately using the Ansible runbook. There is no cascading
Argo deletion or automated prune.

See [acceptance gates](../../docs/central-telemetry-plan.md).
