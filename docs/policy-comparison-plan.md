# Prospective S0/S1/S2 comparison

This is a preregistered protocol, **not completed GPU results**. It addresses the
remaining repeated-policy comparison in the v0.3 specification. The earlier
[single-device smoke test](gpu-experiment.md) and
[transfer comparison](transfer-gpu.md) remain separate historical experiments.

The [frozen machine-readable plan](evidence/policy-comparison-plan.json) defines
all nine policy studies, ordering, seeds, budgets and stopping rules. Regenerate
the same schedule with:

```sh
uv sync --locked --extra optimizer
uv run python examples/plan_policy_comparison.py --output /tmp/new-policy-plan.json
```

Creation time differs on regeneration. The archived plan is the authority for
this trial. Its schedule was independently matched to the experimental-design
skill's complete-block randomization. Each of three temporal blocks contains
lookup, random search and fixed-fidelity qLogNEI once. The randomization unit is
the policy study; Jobs are within-policy repeats and forward blocks are subsamples.

## Workload and comparison boundary

Use the already implemented CUDA CNN runner with fixed generated weights and
input seed, shape 1×3×256×256, FP32, three warmups, 12 measured blocks and 32
forwards per block. The objective includes CPU preprocessing, transfer, forwards
and synchronization; it is not individual request latency. Numerical agreement
must be 1.0 and peak allocated accelerator memory below 2 GiB. This checks a
numerical inference fixture, not task accuracy on a trained-model dataset.

Vary only requested CPU cores 0.5/1/2 and the runner's corresponding thread count.
Each candidate keeps one physical GPU and 2 GiB host memory. CPU-1 is the baseline.
Do not add meaningless combinations to favor BO or enlarge the existing quota.
Fresh runtime/model qualification precedes all studies; evidence from a previous
experiment is not automatically fresh qualification.

First create a new same-workload history using two balanced three-candidate probe
blocks and three independent confirmation Jobs per candidate. Freeze the nine
confirmation profile IDs and their digest for all S0 studies using
[explicit lookup history](lookup-history.md). Later study results and the final
oracle must never enter this cohort. S1 and S2 deliberately start cold; S0's
information advantage and its measured acquisition cost must both be visible.

All policy studies share the same 1,200-second wall and 900 physical GPU-second
first-use ceilings. For S0, those ceilings include the initial history acquisition.
Before starting S0 require history cost plus the reserved 360-second confirmation
budget to fit both caps; stop rather than expand a cap. Subsequent reuse reports
the same historical expense separately and does not charge it again as new work.
Equal caps do not imply equal actual spending or forcing lookup to perform unused
exploration. S1/S2 allow eight probes, preserving the final confirmation reserve.

Each policy independently confirms its predicted finalist and baseline with three
new Jobs each; if both are the baseline, it performs three Jobs. Only after all
nine studies are terminal may a separate finite grid estimate the post-hoc oracle.
That grid is an evaluation expense, never optimizer evidence.

## Reporting and failure rules

Report all selections, abstentions, failures, confirmation outcomes, finite-oracle
regret, wall/queue/run/collection boundaries, optimizer planning time and physical
GPU reservation. Separate history, tuning, confirmation, F0 and oracle costs.
Report first use and reuse across the actual three blocks. Do not extrapolate
production break-even from unmeasured repeated savings.

The maximum is 135 Jobs including three F0 checks, 15 history Jobs, at most 102
policy Jobs and 15 oracle Jobs. Whole-protocol wall time is capped at two hours.
Retain failed attempts; do not replace them after seeing outcomes. Pressure,
expired/lost runtime qualification, thermal eligibility failure or the deadline
stops the protocol. An observation timeout alone is not a terminal backend state.

Three blocks support an auditable operational comparison, not powered method
superiority. An illustrative normal paired-difference calculation at two-sided
alpha 0.05/3 and 80% power requires a standardized difference of about 5.67 with
three blocks. This sensitivity calculation is not the trial's inferential model;
the planned report uses raw paired differences and ranges, with no significance
claim. Its power was independently checked by integrating the normal/chi-square
definition of the [noncentral t distribution](https://docs.scipy.org/doc/scipy-1.13.1/reference/generated/scipy.stats.nct.html).
Direct negative-tail CDF evaluation produced a numerical NaN in the pinned
environment; symmetric survival-tail evaluation matched the independent integral.

The experiment does not establish performance on different workloads, trained
models, accelerators or schedulers. A null improvement is a valid result. The
full [completion audit](goal-audit.md) remains open.
