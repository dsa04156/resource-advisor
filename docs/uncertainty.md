# Recheck recommendation evidence and audit saved forecasts

An approval now rechecks the supporting evidence rather than relying only on the
recommendation's expiry. The same check runs when an approved job is submitted and
again immediately before its first remote scheduler submission. It never cancels
an already running job or changes its resources.

## Versioned reuse policy

`recommendation-reuse-v1` checks the frozen workload digest, current qualified
capability, workload/context signatures, independent supporting profiles, their
age and the recommendation's validity interval. Missing, future, out-of-scope or
expired evidence requires a new check. A profile's content is never overwritten.

The policy also inspects completed follow-up attempts that were submitted **after**
the recommendation was created and match its logical workload and exact execution
context. Supporting runs, pilots, other projects, other contexts and work submitted
before the recommendation cannot act as independent follow-up evidence.

- Three consecutive follow-up times more than 25% above the original mean, or
  three more than 25% below it, trigger `CONSECUTIVE_RESIDUAL_DRIFT`.
- A validated quality/memory violation or an observed OOM/timeout triggers
  `POST_RECOMMENDATION_CONSTRAINT_FAILURE`.
- Once an excursion is observed, the old recommendation stays invalid even if
  later mixed observations return near its mean. A newly supported recommendation
  is required. Running work continues under its existing execution contract.

The 25%/three-run rule is an initial conservative **heuristic**, not an optimal
threshold, statistical change-point test, calibrated safety probability or
diagnosis of thermal/network/contention causes. Sparse observations can miss a
change. Unknown or failed result collection is not a zero-latency measurement.

`lookup-v2` uses the most recent `2 * max(3, minimum_repeats)` comparable profiles.
When every time in the newer half differs from the older half's mean by more than
25% in the same direction, that candidate returns `NEEDS_RECONFIRMATION` with the
triggering attempt IDs. This avoids averaging the detected old and new conditions
into one ranking. Further genuinely measured stable profiles can support a new
recommendation once both recent blocks represent the current conditions. An
independent confirmation study remains another path to a new recommendation.

This bounded recent window deliberately trades some historical sample size for
recency. It does not erase history, automatically run more experiments or bypass
the existing consent, quality, memory, queue and budget checks. The old `lookup-v1`
recommendations and their digests remain unchanged.

## Inspect through the API and console

- `GET /api/v1/compute/recommendations/{ref}/validity` returns the current reasons,
  assessed time, relative residuals and triggering attempt IDs for the owning project.
- The existing recommendation evidence endpoint includes `current_validity`.
  The console displays **재확인 필요** or **조회 시점 검사 통과**, the inspection time
  and expandable reasons. An inspection result is not a performance guarantee.
- `GET /api/v1/compute/uncertainty/shadow-report` returns a read-only audit of saved
  BO forecasts. It does not refit a model, submit work or populate measured profiles.

Reusing a stale approval fails before remote submission. A pending approved job
that becomes stale before the worker submits it fails with a pre-submission reason
and zero allocation; uncertain/already-submitted jobs still follow reconciliation.

## Time holdout with recorded provenance

The shadow report evaluates a forecast only if its immutable ProbePlan was saved
before the target job was submitted, and every listed training run completed
before that plan. The target must not appear among the training run IDs. Project,
workload, plan, candidate, frozen spec and execution-context identities must agree.
Training and target results must be validated hardware records. Duplicate target
attempts are not counted twice. Missing provenance, synthetic data, future data,
invalid intervals and incomparable targets appear under explicit exclusion reasons.

The report gives each workload's held-out count, empirical interval coverage,
mean interval width, absolute relative error and **completed-target** constraint
violation rate. The last rate conditions on comparable completed targets; it is
not an all-attempt reliability rate. Failed/censored executions are not converted
into timing observations, and the exclusions remain visible.

This is a chronological audit of the model snapshots that actually selected
probes. It is not a random train/test split of repetitions or a retrospective fit
that has already seen the target. The existing model is workload-scoped, so the
report explicitly marks cross-workload holdout `NOT_QUALIFIED`. The separately
qualified transfer loop uses target pilots; a time split on one workload does
not establish zero-shot generalization to another. The offline
[workload-holdout evaluation](workload-holdout.md) now tests the source-only rank
prior on complete withheld shapes and audits 24 additional saved forecasts.
That bounded rank evaluation does not provide cross-workload numerical intervals.
A separate [numerical shadow evaluation](numerical-workload-holdout.md) now
withholds all outcomes of each shape and fits only on the other source shapes.
It covers nine in-range Jobs with wide intervals, abstains on 18 out-of-range
Jobs and preserves the chronological audit separately. The live endpoint still
reports its own workload-scoped model; it does not substitute this offline GP.

## Actual historical GPU audit

[Raw evidence](evidence/uncertainty-shadow.json) contains the original plan/attempt
references and training IDs. Two saved qLogNEI forecasts had comparable subsequent
GPU results. The other 39 study observations had no saved forecast and were
excluded rather than assigned a prediction after the fact.

| Metric | Observed value |
| --- | --- |
| Eligible held-out forecasts | 2, one logical workload |
| Actual times inside saved posterior intervals | 0 / 2 |
| Mean interval width | 5.110 microseconds |
| Mean absolute relative error | 1.958% |
| Constraint violations among these completed targets | 0 / 2 |

The small mean error does **not** rescue the narrow intervals: both later
measurements fell outside them. Two correlated observations are too few to
estimate operational coverage. No calibrated 95% guarantee or broadly accurate
uncertainty model is claimed. These are earlier real GPU runs inspected read-only;
no new hardware benchmark or contention experiment was conducted for this audit.

Authenticated HTTPS checks verified the report and an expired recommendation's
recheck response. Chromium verified the existing evidence view at 1440px and 390px.
Regression tests cover residual drift/latching, fresh evidence recovery, queued
submission recheck, quality regression, profile aging, project isolation and
training/target time or identity leakage. Their injected drift scenarios are
synthetic tests, not hardware performance evidence.

Prospective source-only prediction, calibrated intervals and broader load/storage
evaluation remain unproven. Unseen families, runtimes and out-of-range inputs
must abstain, rather than acquire unsupported performance predictions.
The historical two-forecast audit itself
does not qualify R2/R3 algorithms; subsequent transfer evidence is documented
separately in [transfer-gpu.md](transfer-gpu.md).

The optional [container load trace](load-context.md) now implements CPU quota,
throttling and cgroup I/O/PSI collection with result ownership and artifact
propagation. Software tests and a read-only service-container probe are verified;
the separately [preregistered GPU drift sequence](load-drift-plan.md) has now
completed [ten actual CUDA Jobs](load-drift.md), including qualification.
Three consecutive competitor residuals invalidated the saved recommendation;
recovery did not revive it, and its old approval was rejected before submission.
Container counters are not host-wide load or a causal diagnosis, and do not
change the existing drift heuristic or establish broader interval calibration.
