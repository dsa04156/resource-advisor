# Durable optimization studies

`POST /api/v1/compute/profiling-runs` accepts a `StudyRequest` and a stable
`Idempotency-Key`. Read/cancel use the same prefix with `/{ref}` and `/{ref}/cancel`.
The worker advances studies through a durable ask/execute/observe loop.

Requirements: explicit profiling consent, operator-qualified cooperative pilot
entrypoint, compatible baseline, approved mutable fields, bounded probe count,
wall budget, final confirmation reserve and per-device-unit budgets. A device
unit is `backend:device_class:accelerator_model:allocation_mode`; different
devices or physical/virtual allocations are never added as equivalent GPU hours.

Only a persisted active ProbePlan can authorize a pilot job. It records the
candidate, budget, deadline, separate artifact prefix, choice reason, model cost
and model snapshot. The submission key is derived from the plan. Arbitrary
direct pilot requests and changed reservation keys are rejected. Retries after
coordinator restart recover the existing job. Pilot and confirmation use the
same normal backend/Kueue/Slurm paths.

Strategies:

- `lookup`: use existing qualified history, then independently confirm. Without
  history, abstain. It does not secretly perform random search.
- `random`: seeded sampling over the approved finite space under the same budget.
- `qlognei`: bounded initial design followed by actual BoTorch constrained
  qLogNoisyExpectedImprovement. Fit negative log runtime, memory and quality;
  normalize numeric features and one-hot unordered runtime categories. Independent
  repeats estimate noise. Store training run IDs, posterior intervals, versions
  and numerical diagnostics separately from measured profiles.
- `mfkg` / `rgpe`: currently rejected with explicit missing-qualification reasons;
  not aliases for qLogNEI. Their implementation and paired/source-data gates
  remain open in the full completion audit.

OOM/timeouts never become zero-time successful observations. A model failure
falls back explicitly to seeded random selection. Only independent confirmation
runs can create the study's final recommendation; overlap preserves the baseline.
Pilot measurements do not enter the general lookup profile table.

Measured scheduler allocation time is charged when available. Otherwise the
full reserved device time is charged conservatively and labeled as an estimate;
it is not described as actual utilization. Queue/run/collection deadlines request
cancellation. A disconnected backend can delay release: request deadlines do not
prove a physical upper bound on resource occupation. Full failure accounting,
backend deadline granularity/termination overhead, and live cancellation tests
remain required before operational budget guarantees.

Stateful training pilots remain rejected until checkpoint isolation and artifact
transfer are qualified. The GPU runner is not used by the algorithm's synthetic
tests. CPU-only Torch is used for control-plane optimization; actual benchmark
images need their own qualified accelerator framework build.

```sh
uv sync --locked --extra pipelines --extra optimizer
uv run pytest tests/test_study.py -q
```

Tested optimizer versions: PyTorch 2.8.0+cpu, BoTorch 0.16.1, GPyTorch 1.15.2.
See the [real GPU smoke experiment](gpu-experiment.md) for an executed qLogNEI
loop. It retained the baseline and does not show superiority over random search.
Repeated, randomized S0/S1/S2 evaluation remains mandatory.
