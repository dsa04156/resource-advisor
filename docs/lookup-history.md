# Frozen history for lookup studies

New `lookup` studies freeze the same-workload profile IDs and a digest of their
immutable contents when the study is created. Previously, the planner called
lookup against whatever profiles existed when planning began. A delayed study
could therefore acquire evidence from a later experiment. That behavior cannot
support a prospective policy comparison.

To reuse the same cohort across experiments created at different times, pass
`lookup_profile_refs` in the profiling request:

```json
{
  "workload_ref": "qualified-workload",
  "strategy": "lookup",
  "seed": 20261003,
  "lookup_profile_refs": ["attempt-history-a", "attempt-history-b", "attempt-history-c"]
}
```

Omitting the field freezes the currently stored same-workload profiles. An empty
list means no history; it never falls back to the global history. Explicit lists
must contain distinct project-owned profile IDs from the same workload signature,
with a maximum of 4,096 entries. Runtime/context compatibility, evidence age,
hardware evidence policy, minimum independent repeats, uncertainty and independent
final confirmation remain required. Freezing an expired profile does not make it
usable again. It does not import source-workload measurements into a target.

The study's `lookup_history` records the exact cohort and creation boundary.
`lookup_recommendation_ref` links the historical ranking used to choose the
finalist; its own `lookup_history` records the considered IDs and digest. The
final recommendation still comes from new confirmation Jobs. New unrelated
profiles cannot change the frozen historical ranking; fresh confirmation and
approval checks remain necessary before following a recommendation.

Missing or changed cohort contents abstain before dispatch with
`LOOKUP_HISTORY_UNAVAILABLE`. A nonterminal legacy lookup study without frozen
history abstains with `LOOKUP_HISTORY_NOT_FROZEN`; completed historical studies
are not rewritten. Requests without the new optional field retain their original
idempotency digest. Existing random/BO and other strategies reject this option.

`tests/test_lookup_history.py` uses explicit synthetic fixtures to verify later
result exclusion, empty history, cross-study cohort reuse, restart/idempotency,
project/workload isolation, expiration, missing/corrupt evidence and new independent
confirmations. Hardware policy comparisons must provide their own frozen plan and
measured evidence; these tests are not that evidence.
