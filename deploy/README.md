# Deployment boundary

`routes.example.json` is an example, not discovered site configuration. Copy it
outside this repository and configure only authorized project/cluster routes.
The example does not create namespaces, queues, Slurm associations or credentials.

Before enabling a route:

1. Use an independent PostgreSQL database and dedicated service credentials.
2. Kubernetes: provide a restricted kubeconfig, project namespace, existing
   Kueue LocalQueue and an allowlisted qualified node pool. Grant only Job
   create/get/delete and Pod get/list/log in that namespace. No namespace-wide
   administrative credential should be exposed to researchers or workload Pods.
3. Slurm: verify the account, QOS, partition, GRES reservation and device
   isolation independently. Configure key-based SSH with a pinned known host;
   the adapter never disables host-key checking or stores an SSH password.
4. Slurm output must be writable by batch jobs. Use controller-readable shared
   storage, or configure `result_ssh_targets` for verified node-local log reads.
   Native variants require explicit `native_runtimes` bindings; container variants
   are rejected until a container executor is implemented. See
   [Slurm runtime qualification](../docs/slurm-runtime.md). The API host does not
   need a filesystem mount from the Slurm cluster.
5. Keep site addresses, tokens, SSH configuration and private device identities
   outside the repository. Runtime job submission belongs to the worker, not
   ArgoCD. Use TLS before exposing the API beyond localhost.

```sh
export RA_DATABASE_URL='<independent database URL supplied by your secret manager>'
uv run resource-advisor init-db
uv run resource-advisor serve --credentials /run/resource-advisor/credentials.json
uv run resource-advisor worker --config /run/resource-advisor/routes.json
```

Do not use a production database for tests: `RA_TEST_DATABASE_URL` targets only
a disposable `ra_test_*` database. Schema migration, release image build,
production deployment manifests and complete backend qualification remain
subsequent milestones. See [the Kubeflow launch guide](../docs/kubeflow-pipeline.md)
for a rootless launcher build and the TLS/Secret contract.
