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
- `adaptive_replication`: balanced independent initial runs, then additional
  repeats for candidates with unresolved descriptive precision. Fixed work units,
  per-candidate caps, protected final confirmation and explicit stopping reasons;
  see [adaptive replication](adaptive-replication.md). This is not MFBO.
- `fidelity_calibration`: preregistered randomized blocks (three by default) of immutable
  workload/fidelity options, then independent target-only confirmation of every
  configuration. See [fidelity spaces](fidelity-spaces.md). This is a calibration
  experiment, not adaptive MF-KG.
- `mfkg`: [preregistered qualification](qualified-mfkg.md) authorizes a bounded
  homogeneous sampled space. The actual MF-GP/MF-KG chooses configuration and
  fidelity together; a reserved Job executes the option and updates the next
  model. Invalid/missing/expired qualifications are rejected. Numerical failure
  stops exploration and uses new target confirmations. The complete path is
  tested with scheduler doubles; a qualified physical GPU trial remains open.
- `rgpe`: [transfer bindings](transfer.md) join independent source hardware profiles
  to an operator-approved family. Balanced target checks precede actual rank-weighted
  GP ensemble/qLogNEI choices and held-out target confirmation. Invalid sources or
  transfer-model failure fall back explicitly to target-only BO. Missing bindings
  remain rejected; actual GPU transfer effectiveness is not yet demonstrated.
- `history_warm_start`: uses the same source provenance but only orders initial
  candidates by prior ranks, then uses target-only BO. It is separate from RGPE.

[Paired calibration](fidelity-calibration.md) now preregisters a bounded,
randomized F1/F2/F3 measurement schedule and submits its cells through the same
Compute API. It checks independent attempt IDs, workload/runtime invariants,
quality, rank reversal and known-vs-unknown costs. Its identical-input repetition
fixture cannot establish a fidelity-bias model; it does not enable `mfkg` or
claim that fixed replication is adaptive profiling. Adaptive replication is a
separate study strategy with its own request options and trace.

OOM/timeouts never become zero-time successful observations. A qLogNEI model
failure falls back explicitly to seeded random selection; MF-KG instead stops
exploration and attempts protected target confirmation. Only independent confirmation
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
