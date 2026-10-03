# E5 CUDA kernel comparison — instrumentation correction v2

Freeze and publish this amendment, its source and its
[schedule](evidence/e5-kernel-plan-v2.json) before any v2 GPU execution.
The [v1 qualification and one-shot probe](e5-kernel-qualification.md) remain
stopped/completed records with three Jobs and nine GPU reservation seconds.
They are not replaced, pooled into v2 or removed from total experiment cost.

The probe established a parser defect: Kineto emits a same-named GPU annotation
beside each CPU range. Correct the category handling and retain whitelisted
parser inputs before interpretation. This changes instrumentation correctness
and failure evidence only. Do not change the model/input, seed, shape, GPU/CPU
allocation, cache/recompute intervention, sample counts, workload deadline,
quality/thermal limits, bottleneck policy, or acceptance thresholds.

All scientific and execution rules in the [frozen v1 protocol](e5-kernel-plan.md)
apply verbatim to this separate v2 trial, including the original seeded order,
two qualification Jobs, six profiler-off API Jobs and six profiler-on diagnostic
Jobs. The v2 identity includes the corrected source digest; use new immutable
source bindings, Job names, workload references and idempotency keys. A failed
v2 qualification stops this protocol; no replacement or automatic retry.

V2 maximums: **14 new GPU Jobs**, **1260 GPU reservation seconds**,
**1800 seconds from its first qualification submission through final collection**,
90 seconds execution per Job. Reserve the upper bound before the next Job.
Across v1, its probe and v2, report at most **17 Jobs** and at most **1269 GPU
reservation seconds** (the observed nine seconds plus v2's reserved maximum).
Report startup/queue/container/objective separately. Keep profiler timings out
of normal performance histories. Publish all success, failure, missing evidence,
raw trace inputs and final acceptance results without retuning the scenario.
