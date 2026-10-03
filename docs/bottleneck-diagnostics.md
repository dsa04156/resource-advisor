# Cooperative phase diagnostics and the live CNN trial

The service now links serial phase measurements to a completed result and returns
an evidence-bound bottleneck hypothesis. It does not shrink GPU requests, change
a running workload, or infer a cause from low GPU utilization.

## Contract and integration

An optional `phase_profile` accompanies the existing `RESOURCE_ADVISOR_RESULT`
envelope. `ExecutionResult` v1 and its digest are unchanged. Each profile binds
job, attempt, result digest and the workload's measurement boundary. Its bounded
per-work-unit samples must cover the exact result sample/work count and elapsed
time. Durations must be finite, nonnegative and non-overlapping inside each step.
Wrong identities, boundaries, overlapping intervals and late profile replacement
are rejected. Worker ingestion stores result, profile, terminal state and delivery
outboxes in one transaction. The operator-controlled backend collector is the
trust boundary; user-submitted arbitrary telemetry is not a qualification source.

`GET /api/v1/compute/jobs/{job_id}/diagnostics` requires the owning project's token.
The console's execution result column shows the hypothesis and expandable phase
shares. A run with no profile explicitly displays missing instrumentation.
S3/API result bundles contain raw phase samples; MLflow stores phase totals and
diagnostic tags. A phase absent from any sample is not logged as a complete total.
Existing result artifacts without phase profiles keep their original payload.

Policy `serial-phases-v1` requires actual hardware evidence, at least three samples,
80% measured coverage, a group occupying at least 60% of total time and dominance
in at least 80% of steps. Otherwise it abstains. These thresholds are initial
heuristics, not calibrated probabilities or an optimal classifier. The possible
groups are input supply, file I/O, transfer, accelerator execution path and
synchronization. Timings alone do not prove the cause. Missing phases are unknown.
Overlapped/asynchronous pipelines and profiler-enabled results are outside this
serial contract and need separate instrumentation/measurement identities.

## Actual hardware experiment

The trial used one RTX 5080 with PyTorch 2.8.0+cu128/CUDA 12.8, one CPU and 2 GiB
host memory through the existing Kueue/Kubernetes admission path. A fresh immutable
source bundle was qualified before registration. No runtime or cluster upgrade
was required. The independent bounded study worker stopped at study completion.

The model is a deterministically initialized three-convolution CNN and global
average pooling, FP32, TF32 off. Input is a generated `1×3×128×128` tensor. These
are numerical test fixtures; agreement with a CPU reference is not classification
accuracy. Weight, raw-input and prepared-input hashes matched across all eight
application attempts. There are three warmups and 20 timed work units per run.

Only the approved `input_strategy` changed:

- `recompute`: execute the same CPU average-pooling preprocessing every step.
- `cache`: prepare it once and reuse it. The first cache fill is inside the timed
  measurement boundary, so its cost is included. GPU transfer still occurs each step.

Each step measures CPU processing, host-to-device transfer, and synchronized GPU
forward wall time. CPU reference validation, imports, model setup and warmup are
outside this boundary. CUDA synchronization is necessary because GPU execution
is asynchronous; both transfer and forward boundaries synchronize explicitly.
[PyTorch 2.8 CUDA semantics](https://docs.pytorch.org/docs/2.8/notes/cuda.html#asynchronous-execution)

Two exploratory runs were followed by three independent confirmations per setting
in a seeded shuffled order. Each setting retained one GPU and one CPU.

| Setting | Independent confirmations | Mean time for 20 units | Sample standard deviation | Hypothesis in all confirmations |
|---|---:|---:|---:|---|
| Recompute input | 3 | 6.993 ms | 0.171 ms | Possible input-supply bottleneck |
| Cache input | 3 | 2.284 ms | 0.006 ms | Possible accelerator-path bottleneck |

This supports the input-reuse hypothesis for this repeated-input fixture. It does
not prove that caching is valid for changing real datasets, or that GPU computation
itself became faster. Accelerator-path wall time includes launch/synchronization
overhead and is not kernel busy time or utilization. No utilization/power values
were invented. Twenty inner samples do not establish reliable p95/p99 tails.

All eight application attempts passed numerical agreement and Kueue admission;
all eight raw result/phase bundles were read back from S3, authenticated API and
MLflow. MLflow phase totals matched. The application ledger recorded **17 physical
GPU-seconds**, separately from two preceding qualification jobs. These operational
costs greatly exceed the millisecond compute difference for a single run; no net
resource saving or production performance benefit is claimed.

## Reproduction

1. Qualify `python -m resource_advisor.cnn_diagnostic` in a pinned GPU runtime with
   its `contracts.py` and `diagnostics.py` sources. Use an isolated GPU reservation.
   Capture the `RA_CNN_FIXTURE` hashes and result/phase envelope, and validate the
   numerical agreement. Register fresh capability/variant records using those
   observations. The historical public report is not a fresh capability snapshot.
2. Define an identity with that source digest, measured weight digest, generated
   input digest, seed, FP32, shape, work units and boundary
   `cnn-serial-input-transfer-forward-v1`. Set a numerical-agreement quality policy
   with minimum 1.0, three repeats and a qualified memory limit; its digest must
   match the identity. The same command must be the cooperative pilot entrypoint.
3. Generate the consented one-variable workload using the helper below; review
   its 600-second total budget and 240-second confirmation reserve. Register it
   through the authenticated workload API and configure a scoped worker route.
4. POST `{"workload_ref":"your-qualified-workload","strategy":"random","seed":7}`
   to `/api/v1/compute/profiling-runs` with an idempotency key. Save the returned
   study ID before polling. Run the study/compute/delivery workers, inspect that
   same study and its jobs, then retrieve diagnostics and raw artifacts. Do not
   create another study merely because observation timed out.

```sh
uv run python examples/build_cnn_study.py \
  --capability qualified-capability.json --variant qualified-variant.json \
  --identity cnn-identity.json --quality quality-policy.json --output cnn-workload.json
```

[Raw JSON](evidence/cnn-diagnostics.json) contains identities, all 160 application
phase samples, linked results, diagnostics, accounting and artifact delivery checks.
[CSV](evidence/cnn-diagnostics.csv) has one row per independent attempt.

Real storage/network bottleneck interventions, data-loader concurrency, training
checkpoint protection, asynchronous profiling and instrumentation-cost calibration
remain open. This is one E5 controlled GPU scenario, not the entire completion gate.

The [CUDA kernel follow-up protocol](e5-kernel-plan.md) separates uninstrumented
performance from profiler-on kernel evidence and freezes its schedule and budget
before execution. Its preregistration and synthetic contract tests are not GPU
execution results; the original eight-Job evidence above remains unchanged.
The first CUDA trace [qualification stopped](e5-kernel-qualification.md) at a
parser error; its six GPU reservation seconds and unexecuted comparison are
reported separately.
