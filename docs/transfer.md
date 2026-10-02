# R3 transfer: evidence and execution boundaries

`resource_advisor.rgpe` implements a numerical rank-weighted GP ensemble over an
explicit finite numeric configuration space. It cannot submit Jobs or qualify
source workloads. The durable study integration accepts `rgpe` only with an
operator-approved transfer space and an immutable database-derived source
snapshot. Missing or invalid bindings remain rejected.

Two methods are deliberately separate:

* `history_order` uses equal-source rank voting to order initial candidates. It
  fits no GP and is named `history_guided_warm_start`.
* `ask_rgpe` fits independent source GPs and a target GP, estimates ranking losses
  from posterior samples, and uses their winning frequencies as ensemble weights.
  Its acquisition is constrained qLogNEI over registered configurations.

The [versioned BoTorch RGPE tutorial](https://botorch.org/docs/v0.16.1/tutorials/meta_learning_with_rgpe)
describes posterior ranking losses and GP ensemble moments. This implementation
independently implements those operations and adds the following operational
restrictions; these restrictions are heuristics, not claims of optimality.

Each source needs two independent attempts at every configuration. At least three
distinct configurations are required. Target checks are balanced and independent
of source ordering; every candidate needs two target attempts before weighting.
Configuration-level leave-one-out refits drop all repetitions of that
configuration and refit both normalization and GP hyperparameters. Source and
target attempt IDs and workload signatures must be disjoint.

Each task's negative log elapsed time is normalized using its own mean and
standard deviation. Only the target's statistics convert ensemble predictions
back to seconds. Memory and quality constraint GPs use target data exclusively.
Absolute source execution times never enter target measurement records.

Target differences within the declared relative tie band do not establish a
ranking. Sources exceeding the maximum mean rank disagreement, or doing worse
than target leave-one-out ranking, receive zero weight. Equal posterior losses
prefer the target model. If all sources lose their weight, acquisition uses the
target GP alone, and the report names that fallback. A numerical exception
propagates to the caller; it is never presented as an RGPE result.

Reports retain source and target run IDs separately, target leave-one-out folds,
fitted state, normalization, numerical warnings, weights, losses, software
versions, input digest and measured planning time. Prior source evaluation cost
and new target cost are separate. Posterior intervals are uncalibrated and cannot
authorize a recommendation without new independent target confirmation.

## Source family and durable execution

An operator registers `POST /api/v1/compute/transfer-spaces`. The initial scope is
physical-GPU inference/benchmark workloads with identical code, model, execution
environment, entrypoint, precision, work budget, quality contract and physical
node. Only dataset version and bounded input shape may distinguish source tasks;
every shape must already have model-verification evidence. The maximum input
element ratio is explicit (default four). Training, NPU, sampling policies, new
models/SDKs and shared-GPU transfer are outside this initial scope.

CPU and/or host-memory coordinates must distinguish every configuration and are
fixed at registration. Each source and target exposes exactly the same resource
configurations. A changed alias, seed or repetition count is insufficient to
create a distinct source task. Registration freezes workload, variant,
capability and context digests; it grants no execution authority by itself.

A project user binds `POST /api/v1/compute/transfer-spaces/{ref}/evidence`, with
only a new evidence `ref` and `job_ids`. The server joins successful hardware
Jobs, result digests, attempt/epoch, measured profiles, work/quality/memory checks
and thermal traces when configured. It requires two independent measured
profiles per source configuration. Prior source-workload confirmations can be
used; source pilots and this target's Jobs cannot. Callers cannot post elapsed
time arrays. Source capability age is checked at historical submission, while
the target capability must be current. Source results have their own age limit.

The source cohort is selected and frozen before the new study; this is not a
preregistered unbiased transfer experiment. Evidence reuse does not claim source
selection quality or calibrated performance across datasets.

Create a study using the existing `POST /api/v1/compute/profiling-runs` endpoint:

```json
{
  "workload_ref": "target-workload",
  "strategy": "rgpe",
  "transfer_space_ref": "approved-family",
  "transfer_evidence_ref": "prior-source-jobs",
  "seed": 7
}
```

Use a stable `Idempotency-Key`. The approved workload must budget at least two
target probes per configuration **plus one BO step**, and reserve independent
baseline/finalist confirmation. `history_warm_start` instead budgets one initial
probe per configuration plus a BO step. It uses source rank ordering for first
coverage, then the existing target-only qLogNEI loop.

The coordinator rechecks source provenance and freshness at every exploration
step. Loss of usable source evidence, failed target checks or a transfer-model
exception triggers target-only BO with an explicit fallback reason. Ordinary
target compatibility, execution consent and reservation checks still apply.
Every accepted choice creates the existing durable reserved ProbePlan/Job and
the resulting measurement feeds the next fit. Only this study's pilot results
enter its target GP; fresh baseline/finalist confirmations remain held out.
Predictions never enter measured profile history.

Source provenance/normalization/weights are stored with each numerical choice.
Prior source wall time covers only the selected source Jobs, not the complete
upstream search or model-training cost; it is never recharged as target work.
New target evaluation intervals, failed attempts, reservation accounting and
planning time remain in the study's normal ledger/evidence path.

## Verification and remaining gates

`tests/test_rgpe.py` executes real BoTorch fits and acquisition against explicitly
synthetic values. It checks positive source contribution, zero weight for reversed
rankings, target-only fallback for ties, source-time scale invariance, independent
target folds, invalid input rejection, and thread restoration on failure. This
proves numerical behavior, not GPU transfer effectiveness. `tests/test_transfer.py`
also executes the actual GP/acquisition through durable study reconstruction,
reserved Jobs and independent confirmation using an explicit scheduler double.
It tests project/operator access, invalid source provenance, source expiry,
model-failure fallback, and separate source/target accounting.

Still required: real independent GPU source/target trials, browser exposure of
transfer diagnostics, complete upstream source costs, and equal-budget comparison
against cold-start BO.
New hardware, SDKs, model families or training workloads are not implicitly
qualified by a numerical result. No cross-workload speedup is currently claimed.
