# E5 input supply versus CUDA kernel path: measured result

The [fixed v2 protocol](../evidence/e5-kernel-plan-v2.json) completed all 14 GPU Jobs and passed
its three functional acceptance criteria. Three plain runs per condition
distinguished input preparation from the accelerator path; three cache-profiler
runs independently showed CUDA kernels throughout every measured forward range.
GPU allocation stayed at one throughout. No GPU reduction was proposed/applied.

This is a generated repeated-input fixture on one RTX 5080, not evidence that
caching arbitrary production datasets improves them by the same amount. Three
independent Jobs per arm support this bounded functional check, not a powered
superiority claim or population latency tails.

## Actual measurements

Each Job executes 12 measured blocks of four forwards, with input `[32,3,128,128]`,
FP32/TF32 off, seed 20261009, CPU 1 and 2 GiB host memory. All 48 outputs in every
Job agree with its CPU reference at the fixed numerical tolerance. All fourteen
Jobs pass the existing memory and NVML thermal-trace qualification contracts.

Values below are **total objective time per Job**, not end-to-end queue latency
or time for one inference. SD is across three independent Jobs; blocks within
one Job are subsamples. Preparation/warmup/output validation remain in Job cost.

| Input preparation | Profiler | Mean objective | SD | Plain-run hypothesis |
|---|---|---:|---:|---|
| Recompute each forward | Off | 515.146 ms | 2.070 ms | Input supply, 3/3 |
| Reuse fixed prepared input | Off | 122.546 ms | 0.213 ms | Accelerator path, 3/3 |
| Recompute each forward | On | 512.259 ms | 3.689 ms | Diagnostic only |
| Reuse fixed prepared input | On | 125.887 ms | 0.277 ms | Diagnostic only |

Mean plain objective falls 76.21% in this fixture. CPU preparation occupies a
mean 76.20% of recompute objective time; the synchronized accelerator phase
occupies a mean 83.82% of cache objective time. Neither phase share is GPU
utilization. Actual CUDA kernel interval union occupies **99.162–99.195%** of
the cache-profiler forward ranges. This corroborates kernel-dominated forward
execution, not SM saturation, achieved FLOPs or arithmetic-versus-memory limits.

Within the three temporal blocks, profiler-minus-plain differences are
`+3.263, +3.634, +3.127 ms` for cache and `−1.362, +0.515, −7.816 ms` for
recompute. Negative differences are retained as measurement variation; they
do not imply that profiling accelerates the workload. Profiler results never
enter ordinary API performance profiles, recommendation history or MLflow runs.

## Cost and delivery

| Scope | Jobs | GPU reservation seconds |
|---|---:|---:|
| V1 stopped qualification | 2 | 6 |
| One-shot parser-input capture probe | 1 | 3 |
| V2 qualification | 2 | 6 |
| V2 plain API comparison | 6 | 18 |
| V2 profiler diagnostic comparison | 6 | 21 |
| **All retained work** | **17** | **54** |

V2 used 45 GPU reservation seconds and 400.745 seconds from its first
qualification submission through final collection, within the frozen
1260 GPU-second/1800-second limits. Reservation uses scheduled-container
allocation intervals, includes startup and output checking, and is distinct
from GPU busy time or measured objective. Raw per-Job costs preserve the
scheduler's whole-second timestamp resolution.

All six plain API Jobs have one ledger entry, one finished MLflow run, Kueue
admission evidence and byte-identical S3/API/MLflow result bundles. Diagnostic
and qualification allocations are in the trial's separate cost records. The
raw profiler inputs retain every selected CPU/GPU annotation and CUDA kernel;
the auditor recomputes overlap-safe kernel unions independently.

The initial API request for non-baseline cache execution returned 422 without
creating a Job. The comparison uses an explicitly registered fixed cache
baseline for those observations; it does not invent recommendation approval
or disable the guard. This changed registration references only, preserving
scientific identity, context, allocation, schedule and workload. The completed
Job before a transient Kueue reservation-release delay was retained and never
resubmitted. Both setup events remain in the capture.

## Preservation and reproduction

All 536 existing Jobs, 536 usage records, 2357 outbox records, 40 studies and
4111 non-inventory entities remain unchanged. Static object identities/specs,
queue quota, existing Secrets/PVCs and 192 other running Pods are preserved.
The only existing Secret change appends three immutable worker bindings; two
idle-worker reloads load those registrations. Ten nodes remain Ready without
pressure. Kubernetes/KubeEdge versions, drivers and existing workload behavior
were not changed.

- [Raw v2 evidence](../evidence/e5-kernel-v2.json)
- [Recomputed report](../evidence/e5-kernel-v2-report.json)
- [Stopped v1, trace-probe result and parser regression](e5-kernel-results.md)

```sh
uv run python examples/evaluate_e5_kernels.py docs/evidence/e5-kernel-v2.json
uv run pytest -q tests/test_e5_trial.py tests/test_kernel_diagnostics.py
```

The tests reject omitted/reordered Jobs, changed allocation/context/output,
missing CUDA activity, profiler pollution, missing delivery proof and uncharged
allocation. The public replay checks captured evidence; remote service checks
were performed during collection and are not re-executed by that command.

This closes the bounded E5 input-supply/kernel-path lab scenario. Physical
storage/network interventions, asynchronous training pipelines and broader
hardware generalization remain separate work. It does not satisfy the pending
Slurm API/model/device execution requirements or complete the overall platform.
