# Immutable fidelity spaces and mixed-workload calibration

This adds a usable prerequisite to R2: a durable calibration study can execute
registered short and target workloads through the existing Job/worker path.
It does not enable `strategy=mfkg`. A space declaration, policy digest, model
output or successful calibration is not evidence that representative sampling
or thermal behavior is qualified.

## Why each level is a separate workload

Changing the work budget changes the workload signature. Reusing the target
signature for a short run would contaminate performance history and make an
approval ambiguous. A space instead binds each finite option to an immutable
workload, candidate, runtime variant and capability snapshot. It also identifies
the target workload used for independent confirmation and recommendations.

The initial binding contract deliberately supports one homogeneous GPU/NPU group,
inference or benchmark workloads, two to eight configurations, and one to three
lower levels. Every level exposes the same candidate refs and baseline. Only
work units can differ between workload identities: code, model, dataset, config,
shape, precision, seed, batch/global batch, quality and measurement boundary must
remain equal. This is appropriate for a runner whose approved sampling policy is
fixed in its config and whose work budget selects a prefix of that policy.
It is not a generic training-convergence fidelity contract.

Numeric coordinates are derived from approved `host_cpu` and/or
`host_memory_mib` allocations and normalized using saved bounds. They are never
supplied as arbitrary floats or inferred from device names. All other context,
physical node, backend cluster, image, command and compiled artifact must match.
A configuration retains exactly the same resources across measurement levels.
The bounded pilot and normal entrypoints must match; the immutable work budget
controls the level. Both workloads require profiling consent and qualified
accelerator runtimes.

## Register and inspect a space

After separately registering the workloads and variants, an operator posts to
`/api/v1/compute/fidelity-spaces`:

```json
{
  "ref": "example-paired-space",
  "project_ref": "example-project",
  "target_workload_ref": "example-full",
  "lower_workload_refs": ["example-short"],
  "feature_names": ["host_cpu"],
  "fidelity_axis": "identical_input_repetition"
}
```

For a *declared* representative-sampling axis use
`fidelity_axis=representative_sampling` plus `sampling_policy_digest`. The digest
is an identity declaration only; no caller-provided boolean can mark it verified.
The response records `execution_authorized=false` and missing qualification.
Registration replay returns the original immutable space and timestamp.

`GET /fidelity-spaces/{ref}` returns its options and bindings.
`GET /fidelity-spaces/{ref}/options/{option_ref}` resolves both the measurement
binding and its independent target-confirmation binding. This is a read-only
preview: it cannot create a Job or ProbePlan. Every bound capability must still
be compatible for a current preview; an expired capability cannot be relabeled
as current by choosing another option.

## Run the calibration through the ordinary worker

Post to `/api/v1/compute/profiling-runs` with a stable Idempotency-Key:

```json
{
  "workload_ref": "example-full",
  "strategy": "fidelity_calibration",
  "fidelity_space_ref": "example-paired-space",
  "seed": 7
}
```

The target's profiling policy controls the study's total wall and per-device
budgets. It must cover at least three probes per option, every configuration,
and independent target confirmation for every configuration. Registration does
not automatically submit calibration work; this explicit study request does.

Before submission the coordinator saves three seeded randomized complete blocks.
Each option is measured once per block using a separate Job. Two configurations
and two levels therefore yield 12 probes, followed by six new target-level
confirmations when `minimum_repeats=3`. The schedule and space digest are durable.

Each ProbePlan carries its actual workload ref/digest, candidate, option, fidelity,
block, limits and reservation. Submission checks the plan's workload digest; a
caller cannot substitute the target workload for a short-workload reservation.
The target budget protects independent final confirmation. Per-cell run, queue
and collection limits can only reduce the reservation. The coordinator persists
observations and per-device costs and resumes the same active plan after restart.

All probes remain outside measured recommendation history. The provisional
finalist uses only target-level probe results, and all configurations receive
independent target confirmation. A short-run rank reversal cannot make the short
measurement a final performance estimate. Only those new target confirmations
can populate a final recommendation bound to the target workload.

A failed cell, expired/changed execution context, exhausted budget or cancellation
stops further calibration submissions. An incomplete matrix is not promoted to a
completed calibration. This strategy is a fixed experimental design, not MF-KG,
adaptive fidelity allocation or a statistically validated pruning rule.

## Bind measurements for numerical analysis

`POST /fidelity-spaces/{ref}/evidence` accepts only Job IDs and an optional seed:

```json
{"job_ids": ["job-1", "job-2", "job-3", "job-4", "job-5", "job-6", "job-7", "job-8"], "seed": 7}
```

At least two distinct successful hardware-result attempts per option are required.
The service joins project-owned Jobs and immutable results and rechecks attempt,
epoch, result digest, workload/runtime/capability bindings, units, quality,
memory, evidence age and evaluation timestamps. It rejects synthetic results,
arbitrary posted performance values and independent-confirmation Jobs. The
immutable report contains source IDs/digests, normalized per-work-unit results
and Job-creation-to-result-ingestion wall intervals. This interval includes
scheduler and collection delay but excludes compilation or data staging done
before Job creation; it is not GPU busy time or a complete deployment cost.
Historical costs are labeled reused rather than charged as new study work.

For repetition-only spaces, `kernel_input` is always null. For a declared
representative-sampling space, the report provides a numerical `MFKernelInput`
for offline `mfkg-analyze`. It retains `sampling_policy_verified=false` and
`execution_authorized=false`. Analysis does not override the operational MF-KG
rejection or authorize a recommendation. General sampling receipts, thermal
traces, paired-rank qualification and the automatic MF-KG study strategy still
need implementation and actual hardware evidence.

## Verification and limits

`tests/test_fidelity_space.py` exercises the API permissions, immutable bindings,
actual BoTorch kernel on database-bound test envelopes, a complete 18-Job
calibration control flow, coordinator reconstruction, profile isolation,
independent target confirmation, deliberate rank reversal, cancellation,
failed cells, stale capabilities and reservation substitution rejection.
These tests use an explicitly labeled scheduler double and hand-built result
envelopes; they are **not GPU measurements**. Existing historical GPU calibration
and adaptive-replication evidence does not certify this new execution path.
The new mixed-workload coordinator still requires a live deployment trial.
