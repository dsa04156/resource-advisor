# Prospective bounded CUDA load-context and drift acceptance

Status: preregistered before qualification or experimental measurements.
This is a functional fault-injection acceptance sequence, not a powered
performance comparison, a randomized causal estimate or a calibration study.
The service-container read-only probe in [load-context.md](load-context.md) is
preparation evidence and is not a GPU trial.

## Fixed workload and limits

- Existing qualified RTX 5080 lab pool, unchanged Kueue CPU 2/GPU 1 quota.
- One physical GPU, CPU 0.5 and 2 GiB host memory per compute Job.
- `load_observed_benchmark` wraps the existing generated three-convolution CNN,
  FP32, TF32 disabled, shape `1×3×256×256`, seed **20261007**, 12 work units of
  32 forwards, three warmups. Numerical agreement must be 1.0; it is not trained
  model accuracy. Work units, model/input hashes and all context signatures are
  identical across experimental conditions.
- Fixed preparation delay 15 seconds after actual CUDA initialization. Preparation
  and collection remain outside useful compute and inside observed job costs.
- Preserve qualified runtime/driver and existing NVML policy: maximum temperature
  80°C, maximum bracket gap 0.5 seconds and query duration 0.05 seconds. No GPU
  reset, thermal stress, quota increase, driver change or node protection bypass.
- Each Job: no retry, at most 90 execution seconds, 90 queue seconds and
  180 collection seconds. Whole protocol: at most 1,800 elapsed seconds and
  990 GPU reservation seconds. Reserve the worst-case remaining Job allocation
  before each submission; stop new submissions if the budget cannot cover it.

## Fixed sequence and experimental unit

First run **one direct qualification** of the exact immutable wrapper/source and
runtime at these limits. Check CUDA model output, thermal trace, load trace,
actual cgroup limits, identity and scheduler reservation before registration.
If it fails, stop this protocol and retain the failure; no replacement F0.

Only after that pass, submit these nine independent API Jobs, sequentially:

| Slots | Condition | Purpose |
| --- | --- | --- |
| 1–3 | No injected co-runner | Three measured baseline profiles; create one recommendation and immutable approval |
| 4–6 | One bounded CPU co-runner in that same compute container | Observe residuals and actual load-context evidence; retain any absent or mixed slowdown |
| 7–9 | No injected co-runner | Observe recovery and check that an invalid old recommendation cannot revive |

The independent observation unit is the scheduler Job; its 12 blocks are
subsamples. Three runs per phase are fixed by the existing three-consecutive-run
drift rule, not by an assumed detectable effect or an estimate of statistical
power. The intentional A→B→A order tests a state transition and is confounded
with time; it cannot estimate a population causal effect. No significance test,
tail-latency or generalized slowdown claim will be made.

The co-runner is a finite CPU arithmetic loop started only after this experiment's
saved Pod UID emits `RA_LOAD_OBSERVATION_READY`. It runs at most 45 seconds, has
no network/file allocation or CUDA access, and shares the existing CPU/memory
cgroup limit. It changes neither workload requests nor host/node settings. Its
start/end and identity are archived, and its maximum lifetime is independent of
the controlling client. It is an intentional **within-container CPU competitor**,
not evidence of another user's workload or shared-GPU interference. A missing
start marker or an unverified Pod UID is a failed intervention, not permission
to choose another target. Terminate only the saved owned process/attempt if needed.

## Frozen assertions and failure handling

Every successful attempt must have matched model/input signatures, numerical
agreement, memory and thermal gates, a valid cgroup trace, one ledger row and
one MLflow run. S3/API/MLflow result bundle bytes must agree, including the raw
load trace. Record useful compute, container/queue/preparation/collection time,
GPU reservation, CPU counters/PSI/I/O and read overhead separately. Missing
measurements are unknown, never zero or synthetic timings.

The existing heuristic remains three consecutive matching follow-ups >25% above
or below the saved baseline mean. Check validity after every follow-up. If this
condition is actually met, require `CONSECUTIVE_RESIDUAL_DRIFT`; the same old
recommendation must remain invalid after recovery. A fresh lookup during a
detected regime transition must not pool old/new blocks. If the condition is not
met, record a null drift result and do not lengthen stress, lower thresholds or
add observations to manufacture a trigger.

After a verified drift trigger, send **one** old-approval fixed-job request to
test pre-submission rejection. Expected total allocation is ten Jobs (one F0
and nine observations). Reserve an eleventh slot solely for a guard defect: if
that negative request is unexpectedly accepted, immediately request cancellation,
inspect the same attempt to terminal, count its full cost and fail the protocol.
No further experimental submissions are allowed after such a defect.

Any failed API attempt counts and stops new experimental work; no replacement
to obtain three successes. A collection timeout is not terminal and does not
authorize replay: inspect the existing saved handle. Retain failed intervention,
image/setup and process/controller errors, together with all costs. Do not infer
that elapsed observation wait proves a process stopped.

At the end, confirm the dedicated queue and delivery outbox drain, no owned
co-runner remains, all original service/compute identities expected to survive
are preserved, ten nodes remain Ready without pressure, quotas are unchanged,
and all four static Argo applications are Synced/Healthy. The trial cannot
close Slurm execution, unseen-family uncertainty calibration or broad contention
diagnosis; those retain their own acceptance evidence requirements.
