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
separate units. Terminal state and ledger writes are atomic, including failed
collection, cancellation and preflight rejection. Unknown or legacy accounting
remains explicitly unqualified; preparation, energy and utilization coverage is incomplete.

## Current operational limits

- The independent Kubernetes/Kueue GPU path and uncached KFP/API launch/replay
  have real hardware evidence. Bounded Kueue and Slurm quota/priority trials also
  passed; none establishes comprehensive cross-user/device isolation.
- Slurm supports explicitly bound, hash-checked native runtimes. Container
  variants fail preflight until an actual container executor exists. Results
  use shared storage or a verified node-to-SSH mapping; see [the runtime boundary](slurm-runtime.md).
  Its controller is currently unreachable and the complete model/API path is open.
- Model/runtime qualification remains operator-attested. The native guard checks
  listed files/probes, not the completeness of that manifest or signed supply-chain
  provenance. GPU/NPU runtimes must be qualified independently.
- Compute Pods use scoped credentials and operator-qualified read-only runtime
  mounts. This is not a device-isolation or Pod-security certification.
- Bounded JSON results have conditional S3 storage, authenticated API readback
  and MLflow artifacts. Large models, dataset transfer, checkpoint isolation,
  retention, object backup and production storage availability remain open.
- MLflow delivery is retryable, not globally exactly-once. Server-side project
  authorization and failure-run tracking remain deployment/implementation work.
- PostgreSQL backup/restore and fault tests pass. Multi-worker contention,
  real process-crash recovery, disconnects and the complete E0–E7 evaluation
  require further live evidence.
- A small actual GPU random/qLogNEI experiment retained the baseline. Wider
  equal-budget comparisons, MF-KG, RGPE, interference and dashboards remain open.

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
the GPU, checks numerical agreement, and emits the result envelope. It has been hardware-validated on one qualified RTX 5080 runtime. Memory means PyTorch peak allocated device memory;
it is not total board memory. Power, temperature and utilization remain null
until a qualified telemetry source supplies them. It does not implement CNN,
transformer or NPU model benchmarks yet.
