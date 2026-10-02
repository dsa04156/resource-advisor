# Decisions and current limits

## Why an independent service?

The compute infrastructure is shared, but API contracts, source code, database,
credentials, experiment namespace and execution history belong to this project.
This keeps pre-submission recommendations separate from an existing runtime's
placement and migration decisions.

## Why two signatures?

The workload signature describes the same logical experiment: model, data,
precision, work units, quality contract and measurement boundary. The context
signature describes the hardware/runtime/resources used to execute it. A driver
or power-mode change invalidates exact profile reuse without changing the
logical problem. Both are required for a reproducible result.

## Why immutable records?

Replacing a capability or variant under an existing reference could silently
change an approved configuration. Updates use a new reference and a new workload
specification. Capability snapshots have a TTL; stale inventory blocks new
execution. This first version does not resolve stable capability aliases to new
snapshots automatically.

## Why no automatic retry of uncertain submission?

A scheduler can accept a job while the response is lost. The database commits
submission intent before the remote call. Recovery searches the same attempt's
external identity. If acceptance cannot be established, the job stays uncertain
and needs operator resolution; it must not create a second paid workload.

## Why process success is insufficient?

Backend completion enters COLLECTING. The collector then verifies the canonical
JSON digest, schema, attempt epoch, signatures and complete work units. A valid
execution can still fail the quality gate and contribute no recommendation
profile. A prediction is never a result. Production rejects synthetic evidence.

## Why separate queue and measured time?

Compute duration comes from the workload. Allocated device time comes from
scheduler timestamps. They have different boundaries and must not be substituted.
Unknown timestamps remain null. Physical devices and virtual slots remain
separate units. This initial ledger covers collected outcomes; scheduler-only
failures and canceled jobs still need reconciliation into the ledger.

## Current operational limits

- Live scheduler adapters have not yet been validated against a deployment of
  this independent service. Tests do not prove device isolation or quota enforcement.
- Slurm requires explicitly configured account/QOS and a writable shared output
  directory reachable by the controller and worker. It does not assume the
  application host shares that filesystem. Timestamp offsets must be known
  before accounting durations can be computed.
- Model/runtime qualification is operator-attested; no automatic signed
  attestation or artifact/image supply-chain verification is implemented.
- Kubernetes pod security may need device-specific runtime qualification.
  No privileged pod, host mount or service-account token is supplied.
- Result JSON is retained in SQL. Large models, dataset transfer, isolated
  checkpoint copies and object-store artifact upload are not implemented yet.
- MLflow metadata delivery is retryable, not globally exactly-once: a crash
  during external run creation can require duplicate reconciliation. Artifact
  upload and per-project MLflow access segregation remain deployment work.
- PostgreSQL persistence and backup/restore, bounded result collection, and KFP
  compilation are verified in the initial suite. Multi-worker contention,
  process-crash recovery, Slurm accounting, GPU/NPU model execution and real
  quota/priority scenarios require additional verification.
- Pilot and qLogNEI coordination are tested with synthetic execution. Live GPU
  qualification, MF-KG, RGPE, interference optimization and custom dashboards
  remain incomplete; see the full completion audit.

Use separate service accounts, namespaces, LocalQueues, allowlisted node pools,
Slurm accounts/QOS and an independent database. Runtime jobs stay outside GitOps.
Never commit site endpoints, credentials or private documentation.

## Workflow and benchmark integration

`examples/pipeline.py` compiles a CPU-only launcher. The token comes from a
Kubernetes Secret, not a pipeline parameter. A caller supplies a stable run key;
retries reuse it. Caching is disabled. SIGTERM or launcher timeout requests
cancellation; SIGKILL cannot run cleanup, so backend execution/queue deadlines
are also enforced. See the [official KFP caching contract](https://www.kubeflow.org/docs/components/pipelines/user-guides/core-functions/caching/).

`gpu_benchmark.py` is a bounded cooperative PyTorch CUDA matrix-multiplication
runner. It refuses CPU fallback, checks device/runtime identity, synchronizes
the GPU, checks numerical agreement, and emits the result envelope. It is **not
yet hardware-validated**. Memory means PyTorch peak allocated device memory;
it is not total board memory. Power, temperature and utilization remain null
until a qualified telemetry source supplies them. It does not implement CNN,
transformer or NPU model benchmarks yet.
