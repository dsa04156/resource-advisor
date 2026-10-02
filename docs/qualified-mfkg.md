# Preregistered fidelity qualification and the MF-KG executor

MF-KG now has a conditional path through the durable study/Job executor. It
requires a project-owned, unexpired qualification assessment for the exact
immutable sampled fidelity space. Installing BoTorch, registering a space,
completing an arbitrary benchmark or posting a boolean cannot authorize it.
The existing unqualified GPU reports are not retroactively promoted.

## Register the criteria before measuring

An operator first registers the already validated sampled workloads, thermal
runtime variants and [fidelity space](fidelity-spaces.md), then posts to
`/api/v1/compute/fidelity-qualifications`:

```json
{
  "ref": "example-qualification",
  "project_ref": "example-project",
  "space_ref": "example-sampled-space",
  "blocks": 3,
  "tie_fraction": 0.05,
  "maximum_relative_bias": 0.2,
  "minimum_target_elapsed_seconds": 0.05,
  "maximum_age_seconds": 3600
}
```

These defaults are bounded operational acceptance criteria, not statistically
validated universal thresholds. Three independent blocks are a structural
minimum, not a power calculation. Choose replication and observation duration
for the intended workload before the trial. This implementation evaluates
observed consistency; it does not produce calibrated confidence or establish
sustained thermal stability. Higher power/long-duration studies remain separate
validation work. Thermal qualification currently needs the NVIDIA bracketed
trace contract; missing NPU telemetry cannot be replaced with a fabricated zero.

A plan is immutable and can bind exactly one calibration study. With profiling
consent and a stable idempotency key, submit:

```json
{
  "workload_ref": "example-full",
  "strategy": "fidelity_calibration",
  "fidelity_space_ref": "example-sampled-space",
  "fidelity_qualification_ref": "example-qualification",
  "seed": 7
}
```

The study stores a seeded complete randomized block schedule before submitting
Jobs. Every configuration/level is a separate Job in every block. The declared
budget must cover all blocks and independent target confirmations for every
configuration. A second study cannot reuse the plan to select a favorable
trial. Legacy calibration without a plan remains useful evidence, but cannot
be attached afterward. Repeating qualification under a new plan does not justify
an uncorrected statistical significance claim.

## Assess the completed trial

`POST /fidelity-qualifications/{ref}/assessment` derives the assessment entirely
from the bound completed study, reserved plans and validated Job results. The
caller supplies no measurements and cannot omit inconvenient cells. It checks
project ownership, ordered plan/block/attempt association, preregistration
timestamps, sampling receipts, quality/memory constraints and immutable thermal
traces. Final confirmations never enter qualification or surrogate training.

For every candidate/block, compare per-input short and target time. Each ratio
must lie in `[1/(1+b), 1+b]`, where `b` is the preregistered bias limit. Target
forward duration must exceed its declared minimum. For every candidate pair,
classify the log-time ratio as faster, tied or slower using the tie fraction.
Orders must agree between levels and across all blocks. Rank reversal,
unresolved relations, excessive bias or short measurement reject the group.
At least one consistently resolved pair is required; an all-ties fixture cannot
justify enabling a more complicated optimizer. This conservative gate can reject
useful relationships; it is not an optimal statistical ranking procedure.

The immutable assessment reports each paired ratio/order, reasons, source study,
evidence digest, historical calibration cost and expiry. Expiry is bounded by
the oldest source observation and both the plan and workload age policies.
`early_pruning_authorized` remains false: acquisition chooses the next complete
bounded measurement; it does not kill a running workload based on low fidelity.

`GET /fidelity-qualifications/{ref}/assessment` returns both the original
assessment and current eligibility/invalidation. An old `QUALIFIED` record is
not enough after expiry, a context change or observed drift.

## Execute the qualified strategy

After an eligible assessment, submit a new profiling run with the same space
and qualification refs and `strategy=mfkg`. Its budget is independent of the
calibration budget. Prior costs are reported separately and are not charged as
fresh work or hidden as free setup.

The coordinator rechecks qualification and current capabilities, joins the
calibration probes plus only this study's new probes, and calls the real
`SingleTaskMultiFidelityGP`/cost-aware `qMultiFidelityKnowledgeGradient`
[numerical kernel](mfkg-kernel.md). The numerical function itself has no execution
authority. The coordinator converts its chosen option into a reserved ProbePlan
with the exact workload, candidate, fidelity, qualification digest, evidence ref,
model snapshot, estimated cost and measured planning time. The ordinary worker
then submits it through the configured backend and existing queue/quota policy.
The next model sees the newly validated result. Predictions never populate
measured profiles.

Qualification is rechecked at study creation, planning, API job creation and
immediately before the first external backend submission. Recovery of an already
submitted Job continues to reconcile that Job; expiry does not cause duplicate
submission. Failed/OOM/timeout results are not zero-latency training samples.

All observed results must retain the qualified timing envelope and candidate
rank relations. Thermal ineligibility, quality failure, envelope departure or
rank change causes abstention and records a qualification invalidation. Future
studies cannot keep using the old grant. These are conservative observation
checks, not a detector that proves every possible distribution shift absent.

If the model fails, provides no positive KG estimate, or exploration reaches its
budget, the study stops exploration and uses target-level measured evidence for
the finalist. Every configuration receives fresh target confirmations under the
protected final reserve. They are held out from model fitting and are the only
new records eligible for the final recommendation. Model failure is recorded;
it does not create fictional probes. Changed/expired qualification instead stops
submission. A selected configuration still requires the existing approval flow.

## Evidence and remaining limits

`tests/test_fidelity_qualification.py` executes actual BoTorch calls through the
durable coordinator, then uses explicit scheduler/sensor doubles to exercise
mixed workload submission, observation updates, restart boundaries, independent
confirmation, ownership, single-trial binding, rejection, expiry before both
submission stages, drift invalidation and numerical failure fallback.
Hardware-shaped validator fixtures are **not physical GPU measurements**.

The earlier actual [18-Job thermal trial](thermal-evidence.md) verifies the
measurement/delivery path. It predates these preregistered criteria, uses tiny
forward intervals and does not authorize MF-KG. An actual qualified GPU
ask→execute→observe trial and equal-budget S3 comparison remain open. Missing
external preparation costs, lost-process planning cost, sustained thermal
validation and calibrated rank/uncertainty coverage are also not solved here.
