# Noise-aware adaptive replication

`adaptive_replication` is the first level of Notion v0.3 R2. It changes the
number of independent executions of a fixed-work-unit workload. It does not
change shape, data, batch, precision or training progress; it is not MF-KG and
does not qualify a multi-fidelity relationship.

## Request and execution

Register a qualified cooperative workload with profiling consent, an approved
candidate space and explicit wall/device budgets. POST the following to
`/api/v1/compute/profiling-runs` with a stable `Idempotency-Key`:

```json
{
  "workload_ref": "your-qualified-workload",
  "strategy": "adaptive_replication",
  "seed": 20261002,
  "replication": {
    "minimum_runs": 3,
    "maximum_runs": 5,
    "target_relative_standard_error": 0.05
  }
}
```

The workload's `max_probes` must cover `minimum_runs × eligible_candidates`.
Its final-validation wall/device reserve remains protected. Read and cancel use
the existing study endpoints. Every execution uses a persisted ProbePlan,
normal backend submission and queue admission. Replaying a request or restarting
the coordinator resumes the same study and attempt; it cannot create a new
independent sample merely by reading one result again.

## Decision rule

1. Cover all eligible candidates with the minimum independent runs, balancing
   counts and using a seeded choice among ties.
2. For each candidate compute descriptive relative standard error,
   `sample_standard_deviation / sqrt(independent_runs) / sample_mean`.
   Inner benchmark repetitions and request samples do not increase the
   independent-run count. Duplicate attempt IDs are rejected.
3. Among candidates above the requested RSE target and below the per-candidate
   cap, select the largest estimated relative variance reduction from another
   run: `RSE² / (n + 1)`. This assumes stable independent observations and is a
   heuristic, not optimal information gain, expected monetary savings or MF-KG.
4. Stop exploration when precision targets are met, per-candidate limits are
   exhausted, or the existing total budget requires confirmation. Persist the
   distinct reason and candidate statistics; reaching a budget limit does not
   imply a precision target was achieved.
5. Independently confirm the measured finalist and baseline. Existing quality,
   memory and confirmation gates still apply, and overlapping confirmation
   intervals retain the baseline. A repeated failure excludes that candidate;
   an infeasible baseline or changed eligible execution context causes abstention.

Every probe stores the decision statistics, supporting attempt IDs, policy and
planning time. The terminal study retains the final replication assessment,
exploration stop reason, all attempt costs and separate confirmation records.
The assessment that stops exploration also contributes to planning time. Failed
runs are not represented as fast zero-time successes.

## Interpretation and evaluation limits

The experimental unit is one independently submitted Job. A seed makes the
initial tie-breaking reproducible; it does not make actual scheduler timing,
thermal behavior or shared load deterministic. Sampling is adaptive after the
initial balanced coverage, so candidate counts and times need not match.

RSE is a descriptive precision diagnostic. Checking it repeatedly does not give
a sequential confidence level, a powered significance test, a tail-latency
guarantee or a guarantee that candidate rankings are correct. Zero observed
variance in three runs cannot prove a workload deterministic. Independent final
confirmation remains necessary and still has limited coverage on a small lab.

Real comparison against fixed-budget replication and fixed-fidelity qLogNEI
requires preregistered repeated study blocks with equal total budgets including
confirmation, failed trials and planning overhead. Report both null improvements
and abstentions. A single smoke study cannot establish savings or superiority.
Thermal/load-context qualification, cost-aware multi-fidelity modeling and the
equal-budget comparison are separate open gates.

Contract tests use explicit synthetic scheduler doubles. The cases exercise
extra allocation to a noisy candidate, stopping on stable observations, separate
limit/precision outcomes, duplicate rejection, quality/failure exclusions,
changed-context abstention, legacy idempotency and independent confirmation.

## Actual GPU validation — 2026-10-02

The first study used two previously qualified CPU/thread configurations (1 and
2), one physical RTX 5080, the same 256×256 fp32 matmul, three warmups and 100
measured operations per Job. Its immutable request registered three minimum
runs, five maximum runs per candidate, a 5% descriptive RSE target, 12 total
probe cap, 900-second wall budget and a separate 360-second final reserve.
The physical-device budget was 1,200 reservation seconds. Historical F0 checks
are recorded separately in [paired calibration](fidelity-calibration.md).

The study collected three runs per configuration and stopped exploration at six
probes because both observed RSEs met the target (approximately 0.21% and 0.49%).
It then ran three independent confirmations of the selected baseline, CPU1.
All nine Jobs succeeded, used the normal Kueue path and produced byte-identical
API/S3/MLflow artifact readbacks with exactly one FINISHED MLflow run per attempt.
The ledger recorded 18 physical GPU reservation seconds. The study took about
189 seconds including orchestration and waiting; GPU allocation time is not
user wall time. The baseline was retained; this is not evidence of a faster
configuration or of superiority over another strategy.

See the [raw trace and results](../evidence/adaptive-replication.json) and
[per-attempt CSV](../evidence/adaptive-replication.csv). Reported benchmark samples
are subsamples; the nine Jobs remain the independent execution units.

Before this study, an image rollout encountered disk pressure. The scheduler
kept the new management Pods pending. Completed local image-build staging trees
and tar files were removed while build reports and active images were retained;
the node cleared pressure and services became ready before the study was
submitted. No pressure taints were removed and no experiment was submitted
during that recovery. The lab port-forward reconnected after the API Pod changed;
production ingress and automatic storage-capacity management remain open.

A second, separately registered **precision-limit challenge** retained the same
workload and budgets but requested RSE `0.000001` (0.0001%). Its purpose was to
exercise additional real executions and the unresolved-precision limit, not to
establish a useful production threshold or compare optimizer performance.
After the initial six Jobs, the worker selected four more independent probes
using the recorded variance-reduction heuristic. Each candidate reached five
runs while remaining above the requested target. The trace recorded
`REPLICATION_PER_CANDIDATE_LIMIT`, with both `target_met` flags false, then
proceeded to the protected independent confirmation phase. It did not relabel
the unresolved target as achieved.

All thirteen Jobs in that challenge succeeded, including three independent
confirmations of the retained CPU1 baseline. Artifact and MLflow readbacks were
verified for every attempt. It consumed 26 physical GPU reservation seconds and
about 282 seconds of user wall time. See the [strict-target trace](../evidence/adaptive-replication-strict.json)
and [CSV](../evidence/adaptive-replication-strict.csv).

| Execution-path check | RSE target | Probes | Confirmations | GPU reservation seconds | Outcome |
|---|---:|---:|---:|---:|---|
| Precision stop | 5% | 6 | 3 | 18 | Observed precision target met; baseline retained |
| Precision-limit challenge | 0.0001% | 10 | 3 | 26 | Target unmet at repeat cap; baseline retained |

These are two functional checks with different requested precision, not an
equal-budget comparison establishing strategy quality. Across them, all 22
attempt IDs and MLflow run IDs are unique, raw measurements reproduce the saved
RSE summaries, and the usage ledger totals 44 physical GPU reservation seconds.
The final queue was empty and all ten nodes were Ready without pressure.
MF-KG remains disabled: neither result supplies a meaningful fidelity-bias model.
