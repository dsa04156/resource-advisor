# R3 transfer: evidence and execution boundaries

`resource_advisor.rgpe` implements a numerical rank-weighted GP ensemble over an
explicit finite numeric configuration space. It cannot submit Jobs or qualify
source workloads. The `rgpe` study remains disabled until a database-derived
source binding is supplied by a qualified execution integration.

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

## Verification and remaining gates

`tests/test_rgpe.py` executes real BoTorch fits and acquisition against explicitly
synthetic values. It checks positive source contribution, zero weight for reversed
rankings, target-only fallback for ties, source-time scale invariance, independent
target folds, invalid input rejection, and thread restoration on failure. This
proves numerical behavior, not GPU transfer effectiveness.

Still required: immutable operator-approved source/target runtime bindings,
source Job/result/profile provenance and freshness checks, durable study-loop
integration with held-out confirmations and all target costs, real independent
GPU source/target trials, and equal-budget comparison against cold-start BO.
New hardware, SDKs, model families or training workloads are not implicitly
qualified by a numerical result. No cross-workload speedup is currently claimed.
