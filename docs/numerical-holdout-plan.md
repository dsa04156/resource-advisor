# Source-only numerical workload holdout: analysis protocol v1

This is a retrospective, exploratory analysis of the already published
`transfer-gpu.json`, not a preregistered hardware trial. Fix the following
analysis before fitting or inspecting its new numerical predictions. Do not
tune it against held-out results, widen intervals until they cover, or replace
the original saved forecasts. No hardware execution or deployment is involved.

Question: within the one measured CNN/runtime family, can a source-only GP
predict a withheld input shape's duration, and what coverage and interval width
does it achieve? The unit of workload splitting is the complete input shape;
the unit of observation is one independent Job, not a forward/block.

1. Reuse the existing capture auditor and its three complete confirmation
   grids: 128, 192, 256 square pixels, each with three Jobs at CPU 0.5, 1, 2.
   All nine Jobs of the withheld shape stay outside training. The other 130
   captured Jobs remain accounted for but do not enter this balanced analysis.
2. Require identical model/code/config/quality/measurement identity, batch,
   work units, candidate runtime/context and constraints. Only the recorded
   generated-input shape/dataset label differs. This is not an arbitrary image
   resolution or model-family generalization interface.
3. Features are input pixel area and CPU request, each normalized using source
   bounds only. Unknown family/context or values outside those bounds abstain;
   no extrapolative prediction is generated. Requiring the target CPU to be
   present in each source grid also defines the observation-noise lookup.
4. Fit one CPU/double-precision BoTorch `SingleTaskGP` to mean log duration of
   each source shape/CPU cell. Use source-only `Standardize`, default kernel
   and priors from the lockfile, seed 20261003, at most 60 fitting iterations.
   Fixed training variance is within-cell sample log variance divided by
   repeats, with floor 1e-6. Do not treat repeats as independent workloads.
5. Report both latent log-function uncertainty and an explicitly approximate
   individual-Job interval. For the latter add pooled within-cell **individual**
   log variance from source cells at the same CPU (weighted by residual degrees
   of freedom, floor 1e-6). Never use held-out variance. This is an assumption
   that residual variance transfers within this family, not a fitted
   heteroskedastic noise model. It omits variance-estimation and hyperparameter
   uncertainty. Use normal multiplier 1.959963984540054, exponentiate endpoints,
   and label exp(log mean) the predictive median, not the arithmetic mean.
6. Form predictions from source cells and target descriptors before accessing
   target outcomes. Report every raw target duration, mean/SD/median, absolute
   relative point error, coverage and width of both intervals, and observed
   quality/memory violations. Failed source validation or fitting is explicit;
   no alternative model is silently substituted.
7. Include all three folds, explicit abstention counts and null coverage when
   no prediction is made. Record whether all sources preceded target submission.
   Retrospective fits are not forecasts actually saved before execution.
   Keep the existing chronological saved-forecast audit separate and unchanged.
8. No p-values, superiority test or 95% operational guarantee: three correlated
   folds of one CNN on one GPU do not support these claims. Show all raw points,
   including misses. Retain warnings, versions, source digest, fit cost, all
   attempt IDs and original resource cost. No outlier exclusion, model search,
   execution approval, qualification refresh or deployment follows this audit.

BoTorch's [model documentation](https://botorch.org/docs/models) distinguishes
fixed training noise from predicting new observation noise. The additional
pooled variance here is a documented analysis assumption, not functionality
attributed to the GP. Its [API reference](https://botorch.readthedocs.io/en/stable/models.html)
specifies that outcome transforms return the posterior on the original outcome
scale; here that scale is log seconds.
