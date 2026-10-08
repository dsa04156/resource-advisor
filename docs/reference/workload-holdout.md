# Whole-workload rank holdout and saved forecast coverage

This retrospective evaluation reuses the [completed GPU transfer protocol](transfer-gpu.md).
It adds no Jobs and changes no deployed model. The [machine-readable report](../evidence/workload-holdout-v1.json)
contains every fold's training/held-out attempt IDs, raw confirmation timings,
descriptive statistics and every accepted or excluded chronological forecast.

The question is whether a source-only configuration ordering transfers to an
unseen input shape, and separately whether the time intervals actually saved
before later Jobs contain those Jobs' measured durations. These are different
questions: RGPE uses target pilots to learn target scale and weights. Its saved
forecasts cannot establish zero-shot whole-workload interval coverage.

## Design and leakage boundaries

The complete original 157-Job protocol is audited first. Result digests,
workload identities, study/plan ownership, source-profile bindings and execution
timestamps are checked against the published capture. This offline check does
not repeat live S3/MLflow verification or establish current hardware readiness.

The grouped evaluation uses exactly three complete confirmation grids:
128×128 source, 192×192 source and the later 256×256 reference grid. Each contains
three independent Jobs at each of CPU 0.5, 1 and 2: 27 Jobs in total. All other
130 Jobs remain in the original evidence but are outside these balanced grids.
Forward blocks within a Job are subsamples, not additional independent trials.

For each fold, all nine Jobs of one shape are withheld. Only the other two
shapes' 18 confirmations reach the existing `history_order` function. The
target task supplied to that function has **zero observations**. The existing
5% log-time rank tie rule and candidate-reference tie breaking are unchanged.
There is no target normalization, target-dependent weight or hyperparameter
search. A regression test changes every held-out duration while requiring the
source ordering to remain identical. Aliases or seed-only changes do not count
as distinct workloads.

Only the 256×256 fold has all source observations available before every target
Job. The other two folds use later source data and are explicitly retrospective
workload splits, not chronological forecasts. Even the chronological fold was
evaluated retrospectively; this analysis was not preregistered before the GPU
trial. Folds share training data and are not independent replications.

## Source-only rank results

| Entire shape withheld | First candidate in source ordering | Relative excess over best held-out mean | Sources precede target |
| --- | --- | ---: | --- |
| 128×128 | cpu1 | 0.000% | No |
| 192×192 | cpu1 | 0.751% | No |
| 256×256 | cpu1 | 0.914% | Yes |

The source prior assigns cpu1 and cpu2 equal ranks in all three folds. The
candidate-reference tie breaker places cpu1 first. This is the existing first-
probe ordering, **not an independently confirmed production recommendation**.
It matches the user baseline in every fold, so it demonstrates no improvement
over that baseline. All nine held-out Jobs for the selected candidates passed
the recorded numerical-agreement and memory constraints.

Relative excess is selected held-out mean / smallest feasible held-out mean − 1.
It is a finite-sample description, not population regret. Means, sample SDs,
medians and all raw values remain in the JSON; no observations or outliers were
dropped. There are no p-values or superiority claims. These are three shapes
of the same generated fixed-weight CNN on one RTX 5080, not three unrelated
models or representative production datasets.

The rank prior supplies neither target seconds nor intervals. Those fields
remain `null`; an interval is not fabricated from held-out outcomes.

## Chronological audit of actual saved predictions

The same capture contains 24 saved predictions for subsequently executed
candidate Jobs. Each training result was recorded before the plan, and each
plan precedes its target's backend submission. Target pilots and transferred
source confirmations retain separate IDs. The remaining 133 observations had
no saved prediction and are listed as exclusions, not backfilled by refitting.

| Strategy | Eligible / studies | Actual duration inside saved interval | Mean interval width | Mean absolute relative error |
| --- | ---: | ---: | ---: | ---: |
| qLogNEI | 10 / 2 | 8 / 10 | 34.933 ms | 2.341% |
| History warm start + target BO | 10 / 2 | 8 / 10 | 20.533 ms | 0.493% |
| RGPE | 4 / 2 | 0 / 4 | 0.695 ms | 0.659% |

No eligible completed target violated the recorded quality or memory limits.
This rate is conditional on these completed targets, not an all-attempt failure
rate. Duration means the complete 12-block objective, including preprocessing,
transfer and 384 CUDA forwards, not individual request latency.

The saved intervals describe the models' **latent posterior**. They were not
calibrated predictive intervals for noisy future Jobs. In particular, all four
RGPE observations missed their narrow intervals despite small point errors.
This is evidence against treating these intervals as an operational coverage
guarantee; it does not isolate whether noise, numerical approximation or drift
caused each miss. The report preserves individual residuals for follow-up.
Two correlated studies per strategy cannot establish a 95% coverage rate or
justify widening intervals until this same dataset passes.

## Reproduction and remaining scope

```sh
uv sync --locked
uv run python examples/evaluate_workload_holdout.py \
  --input docs/evidence/transfer-gpu.json \
  --output /tmp/workload-holdout-audit.json
```

The output must not already exist. The rank calculation needs no optimizer
installation, model fitting, credentials or live service. Tests reject target
leakage, duplicate training IDs, modified result digests, chronology errors,
infeasible sources and ambiguous forecasts. Missing/invalid saved intervals
remain explicit exclusions; nonfinite JSON evidence is rejected.

New GPU reservation is zero. Reused original application cost is 437 GPU
reservation seconds, including 82 for full source characterization. The
original trial's 38 qualification seconds remain in its separate accounting,
for 475 seconds overall. Reuse is not a claim of saved runtime or free history.

This closes a bounded **source-only rank** workload-holdout check and expands
chronological forecast evidence. A separate
[numerical shadow holdout](numerical-workload-holdout.md) now predicts the middle
shape with wide intervals and explicitly abstains on both outer shapes. It does
not validate unseen model/runtime families or calibrated future intervals.
A later [bounded live drift trial](load-drift.md) independently verifies
stale-recommendation latching and old-approval rejection; it does not calibrate
these saved intervals. This holdout audit does not activate stale qualifications,
approve execution, remove confirmation gates or modify the running platform.
