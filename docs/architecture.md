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

The [completion audit](goal-audit.md) maps each required gate to its current
evidence. A bounded live test qualifies only its stated workload and failure
window; it does not qualify the whole backend or a production deployment.

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
  and MLflow artifacts. [Isolated deterministic training](training-isolation.md)
  also verifies unchanged input/checkpoint originals and separate restored
  outputs. General training, large checkpoints/models, dataset transfer,
  retention, object backup and production storage availability remain open.
- MLflow delivery is retryable, not globally exactly-once.
  [Resultless failure/cancellation tracking](failure-tracking.md), connection
  refusal and accepted-create response loss have live evidence. Direct
  server-side tenant authorization and model registration remain open.
- [Independent PostgreSQL restore](restore-rehearsal.md),
  [accepted-submit crash recovery](worker-recovery.md),
  [cancellation evidence retention](termination-retention.md) and
  [two concurrent workers](concurrent-workers.md) have bounded live evidence.
  Expired-lease external-service fencing, disconnected-node accounting, Slurm
  recovery and the complete E0–E7 scenario matrix still need verification.
- Actual fixed-fidelity qLogNEI and [RGPE/warm-start transfer](transfer-gpu.md)
  loops have live evidence; the transfer comparison did not demonstrate a
  selection advantage. A [new equal-budget S0/S1/S2 comparison](policy-comparison-v2.md)
  is in progress after a stopped trial exposed missing optimizer dependencies.
  The repaired worker now requires those dependencies before startup. Physical
  multi-fidelity qualification, broad transfer effectiveness, calibrated
  uncertainty and shared-device interference remain open.
- The [four-view console](console.md) exposes scoped inventory, jobs, queues,
  accounting and recommendation evidence. [Slurm host telemetry](slurm-inventory.md)
  is live independently of controller availability; controller queues and
  reservations remain unknown, not zero. Broad production observability and
  direct MLflow/S3 tenant access controls remain open.

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
transformer or NPU model benchmarks itself. A separate generated CNN fixture
has qualified CUDA execution and [execution-bound thermal evidence](policy-comparison-v2.md);
its exact numerical agreement is not trained-model accuracy. Two separate
real Hailo model qualifications [failed their fixed quality gates](hailo-efficientformer.md),
so the presence of a working device/runtime has not enabled NPU recommendation
or service execution.
