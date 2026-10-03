# Offline replay of the confirmation uncertainty gate

This S5 analysis asks whether removing only uncertainty-based baseline retention
changes the final recommendation. It is exploratory analysis of an already
designed experiment, not an additional preregistered hardware treatment. It does
not change the running S0/S1/S2 experiment, fit a model, submit a Job or produce
new execution evidence.

The [evaluator](../examples/evaluate_uncertainty_ablation.py) first runs the full
completed-policy-capture audit. Both replayed policies use the same recorded
finalists and independent confirmation attempts. Quality, peak-memory, approved
candidate, repeat-count and chronological checks remain mandatory. Pilots and
untested alternatives never become confirmed finalists. Existing terminal
abstentions remain visible rather than being silently converted into decisions.

The strict replay reconstructs the service's final confirmation rule: compare
mean execution times, but retain the baseline when the best candidate's upper
bound overlaps the baseline's lower bound. These bounds use the original
`mean ± 3 * sample SD / sqrt(n)` heuristic; they are not calibrated 95% confidence
intervals. The replay must agree with the saved actual selection. The second
policy chooses the smallest feasible confirmation mean without this overlap
test. Exact mean ties retain the baseline. This removes one decision gate;
it does not reconstruct an alternative search trajectory.

Each decision reports raw confirmation values, mean/SD, attempt IDs, both selected
configurations, and their later independent reference mean/regret. The reference
runs occur after all policy decisions and never enter the replay's selection.
Their means are finite-sample estimates, not known true performance or labels of
statistically proven error. Report raw paired decisions without p-values or a
superiority claim; the three temporal blocks use one device and one workload.

Distinguish **decision coverage** from **nonbaseline recommendation frequency**.
Keeping a qualified baseline is a valid decision, not missing data. Both rates
use all nine policy studies as their denominator. A changed counterfactual
selection is not a measured deployment improvement or a demonstrated cost saving.
The original measurement cost is retained; offline replay creates no new GPU Jobs.

For a complete capture:

```sh
uv run python examples/evaluate_uncertainty_ablation.py \
  --capture docs/evidence/policy-comparison-v2.json \
  --plan docs/evidence/policy-comparison-plan-v2.json \
  --output /tmp/new-confirmation-ablation.json
```

Current verification covers synthetic replay contracts: quality/memory/failure
and repeat gates, unchanged input, duplicate/future/unapproved evidence rejection,
preserved abstention and exclusion of pilot-only candidates. Actual completed
capture replay is now verified in [the raw report](evidence/uncertainty-ablation-v1.json):
all nine recorded selections matched the strict replay and 0/9 changed when the
uncertainty overlap gate was removed. Both modes had 9/9 decision coverage, 8/9
nonbaseline recommendations and no promoted invalid candidate. No recorded
decision used uncertainty-triggered baseline retention, so this null difference
does not show that the gate is unnecessary. The third qLogNEI study confirmed
only its predicted baseline; later reference runs cannot retroactively add an
untested finalist to its selection. No new GPU Job was created, and the existing
361 GPU reservation seconds remain attributed to the original experiment.
This one-workload replay cannot establish numerical cross-workload coverage or
forecast-interval calibration. The separate
[numerical shape holdout](numerical-workload-holdout.md) now measures bounded
interpolation coverage and out-of-range abstention; its broad intervals do not
establish calibration. The later
[bounded induced-drift trial](load-drift.md) verifies stale-approval rejection
without changing this offline ablation or calibrating its intervals.
