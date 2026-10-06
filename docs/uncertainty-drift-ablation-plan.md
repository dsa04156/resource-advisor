# S5: chronological offline drift-gate ablation

This is exploratory analysis of the already published
[fixed-order load-drift trial](load-drift.md), not a new prospective hardware
treatment or an unseen dataset. Earlier means and drift outcomes are already
known. Freeze the replay definition before computing its paired decisions.
Do not change the existing policy, thresholds, source capture or live services.

Question: what changes when ONLY `CONSECUTIVE_RESIDUAL_DRIFT` is removed from
the recorded recommendation-reuse reasons? Preserve qualification, identity,
memory, quality, repeat, chronology and every other recorded reason. Compare
the same already qualified candidate and frozen baseline forecast; this is not
an alternative optimizer search or a resource-selection regret experiment.

Use all SIX follow-up Jobs: three CPU-competitor observations and three normal
recovery observations. The initial three normal Jobs are support evidence only,
not held-out targets; the separate qualification remains a charged predecessor.
Never choose a cohort by whether a later result looks favorable.

For each target, use only the baseline recommendation and validity assessment
available BEFORE its submission. In particular, a target's own result may
invalidate reuse only for the NEXT target. The first follow-up has the freshly
created qualified recommendation; each later follow-up uses the previous
completed/verified follow-up's assessment. Check timestamps and evidence IDs.
Do not use the current/final stale state for earlier targets or let the known
recovery outcomes revive the old recommendation.

Keep later target results for evaluation only. Report paired reuse/abstention
decisions, all denominators, raw elapsed values, quality/memory outcomes, error
against the frozen mean, inclusion in the ORIGINAL heuristic interval and
absolute residual exceeding the original25% threshold. A forecast exceedance
is not proof that the resource configuration was wrong. A replayed permission
to reuse is not an actually approved/submitted additional workload.

Report error/coverage separately for all six targets and each policy's accepted
subset; the latter differ and cannot support a causal superiority claim. Include
phase n/mean/SD/median, raw sequence and policy masks. No significance tests or
population confidence intervals: one fixed-order device/workload sequence is
confounded with time and intentionally induced load. The interval is the original
mean±3 sample standard errors, not a calibrated future-Job guarantee.

Run the existing full load-drift auditor before replay, recompute baseline
support/interval and require recorded policy/assessment consistency. Refuse
missing/duplicate/foreign/future evidence, altered result digests or omitted
costs. Keep all197 original GPU reservation seconds and zero new GPU Jobs;
do not book hypothetical avoided execution as measured savings. The older
[confirmation-gate ablation](uncertainty-ablation.md) remains a distinct cohort
and gate, rather than pooling its rates with this reuse-gate replay.
