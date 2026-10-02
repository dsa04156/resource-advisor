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
