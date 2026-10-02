# Multi-fidelity GP and cost-aware KG: numerical implementation

The `mfkg` execution strategy is still disabled. This increment implements and
tests its numerical model/acquisition kernel and a runnable analysis CLI. It
does **not** establish a qualified hardware fidelity axis or complete the
mixed-fidelity study-to-Job integration. The existing matmul repetition experiment
cannot be relabeled as multi-fidelity training data.

The implementation uses the installed **BoTorch 0.16.1**
`SingleTaskMultiFidelityGP`, `qMultiFidelityKnowledgeGradient`, target-fidelity
projection and `InverseCostWeightedUtility`. See the
[versioned official tutorial](https://botorch.org/docs/v0.16.1/tutorials/multi_fidelity_bo).
Dependencies and cluster versions were not upgraded.

## What the kernel computes

`MFKernelInput` describes one declared homogeneous runtime group, named numeric
coordinates normalized to [0,1], a finite set of configuration/fidelity options,
and independent repeated observations. Each configuration must have a target
option at fidelity 1 and at least one lower-fidelity option. Every observation
must match the group's signature and a registered numerical option. Duplicate
attempt IDs, missing target options, mixed dimensions, failed quality/memory
checks, nonfinite measurements and nonpositive costs are rejected.
Every supplied option currently needs at least two independent observations;
this kernel reallocates measurements within an already observed finite group.
Cold-start sampling of new configurations and estimating their unknown costs
still require an operational initial-design policy.

This is a numerical input contract, not hardware attestation. The declarations
still need to be bound to actual immutable workload, environment, sampling and
quality evidence before execution can be enabled. A runtime name or device model
must not be encoded as an ordered number in these numeric coordinates.

For each option, fit the mean negative log of seconds per work unit. Repeated
independent runs estimate variance of that mean, with a recorded numerical floor.
The GP's fidelity covariance models the relationship between measurement levels.
Simply increasing the number of repetitions is not accepted as the declared
fidelity axis.

For every possible next configuration/fidelity pair:

1. Sample hypothetical observations using a seeded Sobol sampler.
2. Condition the fitted MF-GP on each hypothetical observation.
3. Enumerate the finite approved **target-fidelity** configurations and choose
   the best posterior mean for each fantasy. Neither the terminal choice nor
   the next measurement can drift into an unregistered interpolated resource
   configuration.
4. Pass the proposed measurement and those fantasy maximizers to the actual
   BoTorch MF-KG acquisition. Subtract the current best target posterior mean
   and apply the cost utility.
5. Return the highest positive finite acquisition estimate, or an explicit
   no-positive-estimate result. Fitting errors propagate; they are not presented
   as a random choice made by MF-KG.

The finite inner maximization is exact over the provided target options. The
expectation remains a finite Monte Carlo approximation; it is not a continuous
global optimum or proof of real performance improvement. This implementation
uses q=1, one group per call, and bounded option/observation/fantasy counts.

## Cost and prediction semantics

The cost table is the mean observed total evaluation wall time for each option.
There is no extrapolation to unmeasured-cost configurations in this first kernel.
Callers must eventually prove that these intervals include preparation, data
transfer, queueing, execution and collection under the intended cost definition.
The numerical fixture supplies synthetic costs explicitly.

Costs are divided by the mean target-level cost before entering BoTorch's
piecewise utility. Positive fantasy gains are divided by that dimensionless
cost; nonpositive gains are multiplied by it. The reference and raw seconds
are retained. This makes a uniform seconds/minutes rescaling leave selection
unchanged. The acquisition value is **not** measured seconds saved, GPU-hours
saved or an ROI estimate. Cost uncertainty is not modeled.

Actual local GP-fitting and acquisition computation times are reported separately.
They are not silently added to historical evaluation durations or described as
GPU work. A future operational coordinator must charge these times against the
study's total budget and retain its independent final-confirmation reserve.

Predictions are explicitly unmeasured target-level medians and posterior intervals.
They never enter the measured profile table. Reports include input digest,
training attempt IDs, aggregated training arrays, variance floor, seed, package
versions, warnings, fitted model state and its digest. Infinite **constraint
bounds** in the state are encoded as signed strings for strict JSON; nonfinite
fitted values are rejected. Posterior intervals remain uncalibrated.

## Reproduce the numerical checks

```sh
uv sync --locked --extra optimizer
uv run pytest tests/test_mfkg.py -q
uv run python examples/mfkg_numerical_demo.py --output /tmp/mfkg-demo.json
```

The demo generates a mathematical fixture with explicit fidelity-dependent bias,
then performs three genuine GP-fit → joint choice → new synthetic observation
steps. It does not invoke a GPU workload, Kubernetes, Slurm, MLflow or a database.
An existing output file is not overwritten. Use a new path for another report.

To analyze a separately prepared `MFKernelInput` JSON:

```sh
uv run resource-advisor mfkg-analyze \
  --input /path/to/mf-input.json --output /tmp/mf-analysis.json
```

The command returns `execution_authorized=false` and exits before opening the
application database or constructing scheduler backends. It cannot create a
ProbePlan or authorize a Job. Tests verify that boundary, actual BoTorch scoring
against an independent finite-fantasy calculation, target-only projection,
cost sensitivity, cost-unit invariance, invalid data rejection and fit failure.

The [saved three-step trace](evidence/mfkg-numerical.json) is labeled
`synthetic_numerical_fixture`. It is implementation evidence, **not GPU benchmark
evidence** and not an equal-budget strategy comparison.

## Remaining operational gates

- Meaningful representative-sampling policy and actual paired low/target data
  with unchanged shape, distribution, precision, batch and runtime assumptions.
- Qualification of ranking/bias, quality, memory, thermal behavior and context
  stability; repetition-only or rank-reversing groups remain ineligible.
- Verified cost intervals and their uncertainty; finite-space group selection.
- Durable mixed-fidelity ProbePlans, workload/result bindings, per-device budget
  accounting, cancellation/recovery and independent target confirmation.
- Held-out calibration and real equal-budget comparison with fixed-level qLogNEI.

Until those gates have direct evidence, `Studies.create(strategy="mfkg")` keeps
its existing explicit rejection. Numerical correctness does not authorize
running an unqualified fidelity policy on the shared cluster.
