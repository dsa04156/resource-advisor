# Actual GPU smoke experiment — 2026-10-02

This verifies the execution and recommendation path on one GPU. It is not a
scientific comparison of optimization methods or a production certification.

The path was authenticated Compute API → independent PostgreSQL → durable
worker → suspended Kubernetes Job → Kueue admission → RTX 5080 CUDA execution
→ result schema/signature/quality validation → profile/confirmation → MLflow.
KFP was not involved in this experiment; its live acceptance gate remains open.

## Fixed workload and allowed change

One NVIDIA GeForce RTX 5080; PyTorch 2.8.0+cu128, CUDA 12.8; FP32 matrix
multiplication of two 256×256 matrices; seed 0; three warmups; 20 timed iterations.
The timed boundary is synchronized forward execution, excluding initialization
and the CPU double-precision numerical reference. Every collected run passed the
numerical agreement check. This quality check is not model accuracy.

Only CPU requests varied: one versus two CPUs. GPU count, input, work units,
precision, memory request and environment stayed fixed. Power mode was not
controlled; GPU utilization, power and temperature were not collected and remain
null. Tiny matrix operations emphasize launch overhead and are not a throughput
qualification for a saturated GPU.

Each study had the same 600-second wall ceiling, 600 physical-device-second
budget, at most four exploration probes and a reserved 180 seconds for independent
confirmation. Budgets are ceilings, not equal realized costs. The three strategies
ran sequentially with seed 7 and shared initial historical observations.

## Results after the queue-budget correction

| Strategy | Exploration / confirmation jobs | Result | Confirmed configuration | Recorded allocation charge |
|---|---|---|---|---|
| History lookup | 0 / 1 | Abstained after queue deadline | None | 27 s conservative reservation; actual allocation unknown |
| Random search | 4 / 6 | Completed | Baseline: one CPU | 22 s from scheduler allocation intervals |
| qLogNEI | 4 / 3 | Completed | Baseline: one CPU | 15 s from scheduler allocation intervals |

Random search proposed two CPUs, but independent confirmation intervals overlapped,
so it retained the baseline. Baseline mean timed work was 0.000271528 s; two CPUs
were 0.000274653 s. qLogNEI selected the baseline, then confirmed it with three
new jobs (mean 0.000272181 s). These descriptive values do not establish a speedup.

Recorded whole-study wall times were 91.005 s for random and 66.905 s for qLogNEI;
optimizer planning used 0.000135 s and 1.172 s respectively. Different confirmation
counts explain part of the difference. This is not evidence that one strategy is
cheaper or better in general. Preparation and all failure costs are not yet in a
complete ledger.

The initial implementation left only one second for confirmation queueing, causing
all three initial studies to abstain. The defect was corrected and regression-tested.
The subsequent lookup still exceeded its 27-second queue allowance; no Pod-scheduled
timestamp was recorded. Its exact controller/startup delay was not established.
The failure was retained rather than converted to a successful measurement.

## Evidence and tracking

- [Raw study JSON](evidence/gpu-study.json): both rounds, all 29 study attempts,
  plan limits, actual observations, errors, charged-cost sources and recommendations.
- [Flat CSV](evidence/gpu-study.csv): the same attempt measurements and failures.
- [CUDA F0 evidence](evidence/cuda-f0.json): separate 4,096-element kernel validation.
- MLflow: 28 real completed runs, including three initial observation jobs; all
  28 delivery events are DONE and all runs have durable links in the independent DB.
  The four canceled study attempts have no valid measured result and no completed
  MLflow run. This gap is visible in the study data; failure tracking remains open.

MLflow parameters include image, environment digest, runtime versions, logical
workload identity, resource requests and allocation mode. Metrics exclude null
telemetry. Attempt, job, study, evidence kind, quality and signatures are tags.
Actual start/collection timestamps are reused across retries. Artifact upload is
not yet verified. No site address, hostname, credential or private dashboard is
included in these public files.

## Reproduction prerequisites

1. Install the locked control-plane environment with `uv sync --locked --extra
   pipelines --extra optimizer`; run a dedicated DB, authenticated API and worker.
2. Provision a lab-only LocalQueue/ClusterQueue/flavor and a namespace-scoped worker
   credential. Ensure the namespace matches the installed Kueue controller's managed
   namespace selector; never bypass admission by unsuspending the Job yourself.
3. Register a freshly qualified CapabilitySnapshot, pinned RuntimeVariant and
   consented WorkloadSpec. The GPU runtime must support the actual hardware. The
   optional operator-configured runtime bundle mounts qualified packages and source
   read-only; it is not automatic image attestation.
4. Submit three independent baseline `observe` jobs with distinct stable idempotency
   keys. Submit `lookup`, `random` and `qlognei` studies through
   `POST /api/v1/compute/profiling-runs`. Poll each study to a terminal state.
5. Configure `mlflow_url` and explicit `mlflow_experiments` project-to-experiment
   mapping in the private worker configuration. Unmapped projects remain pending;
   they never fall through to experiment 0. Server permissions must independently
   restrict the worker's MLflow access.
6. Export all attempts, including errors, and distinguish timed kernel work from
   scheduler allocation, total wall time and conservative cost reservations.

The delivery adapter follows the [MLflow REST API](https://mlflow.org/docs/latest/api_reference/rest-api.html).
Site-specific credentials, node bindings and private operational scripts are not
part of this public report. Portable deployment automation, broader workloads,
NPU/Slurm qualification and randomized repeated comparisons remain required.
