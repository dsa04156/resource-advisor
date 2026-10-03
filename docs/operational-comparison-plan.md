# B0/B1/B2 operational comparison: prospective protocol

Status: **frozen before execution; [completed results](operational-comparison.md) are now published**. The
[frozen machine-readable plan](evidence/operational-plan-v1.json) and
[reproducible allocation generator](../examples/plan_operational_comparison.py)
precede qualification and measurement. This comparison is separate from the
completed S0/S1/S2 tuning-policy trial.

| Arm | Actual execution path | Resource choice |
| --- | --- | --- |
| B0 | Standard Kubernetes Job, existing Kueue queue, raw result/log collection | Qualified user baseline: cpu1, physical GPU 1, host memory 2 GiB |
| B1 | Advisor API observe mode, compatibility/identity checks, worker, validated result and linked accounting/artifacts/MLflow | Identical cpu1 baseline; no recommendation applied |
| B2 | B1 infrastructure, fresh fixed-fidelity qLogNEI profiling, independent confirmation and immutable approval, then fixed-mode execution | Confirmed candidate from cpu0.5/1/2; the baseline remains a valid outcome |

All arms use the same generated fixed-weight CNN, 256×256 input, FP32,
seed 20261004, three warmups and 12 measured blocks of 32 forwards. Quality is
exactly the existing numerical-agreement contract; it is not trained-model
accuracy. Same image, runtime/source bundle, physical GPU, queue, priority,
memory, thermal policy, phase collection and external observation cadence are
required. Qualification warms the image/runtime; this is not a cold-cache trial.

Six temporal blocks contain each arm once in a seeded random order. The
allocation matches the experimental-design skill's `block_randomization`
algorithm with block size 3 and seed 20261004. Jobs are repeated observations
on one device/workload, not independent GPU populations. One tuning episode
is completed before main runs; its results and approval are frozen. No main
run can influence the profiler's choice. There is no hypothesis test, powered
superiority claim or outcome-dependent sample-size change.

The trial permits three fresh F0 Jobs, at most 14 profiling/confirmation Jobs,
and 18 main Jobs: at most 35 total. It preserves the existing quota CPU 2/GPU 1.
Main execution is capped at 30 seconds, queue wait 90 seconds, result collection
60 seconds. The whole trial is capped at one hour and 1,500 physical GPU
reservation seconds. The profiling study retains its own 1,200 second wall,
900 GPU-second and 360-second confirmation-reserve limits.

Report raw scheduler and container timestamps, measured compute duration,
quality/memory outcomes, NVML query overhead, observed API/client durations
and artifact completion separately. The common endpoint is request to raw
result availability for every arm. B1/B2 additionally report request to API
validation and linked delivery. Their extra path includes worker polling,
storage and delivery; its entire wall difference must not be presented as the
CPU cost of the validator alone. B0 retains an independently auditable raw
record outside the platform database; it does not silently receive B1 features.

For each actual repeat count N=1..6, show serial cumulative main-run wall time
and resource cost. B2 additionally pays the complete fresh profiling,
confirmation and recommendation/approval preparation cost once. All F0 costs
are separately visible as shared setup. Report CPU core-seconds, host-memory
MiB-seconds and physical GPU seconds separately without invented prices.
Do not extrapolate a break-even point from a small timing difference. The
overall interleaved experiment makespan is a separate measurement.

Preserve abstention, failed pilots and failed main runs. A failed/abstained
profile can lead only to an explicitly recorded baseline fallback; it cannot
manufacture a recommendation or become a successful tuning result. A main
failure remains in the denominator and is not replaced. Stop new submissions
on node pressure/disconnection, runtime or quota drift, unknown allocation,
thermal failure or global budget exhaustion. An observation timeout requires
reconciliation of the same saved IDs, not a new submission. Expiry and quality
gates cannot be relaxed to finish the trial.

Reproduce the allocation without executing hardware work:

```sh
uv sync --locked --extra optimizer
uv run python examples/plan_operational_comparison.py \
  --output /tmp/operational-plan-v1.json
```

Site credentials, node routing, qualification results and private raw records
belong outside the public repository. A final report must distinguish measured
effects from absent improvement, incomplete stages and remaining limitations.
