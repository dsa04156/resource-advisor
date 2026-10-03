# New policy trial after the worker runtime repair

This is a new prospective experiment, not a resumption or replacement of the
[stopped first protocol](policy-comparison.md). Its [frozen plan](evidence/policy-comparison-plan-v2.json)
retains the same workload, CPU candidates, single physical GPU, seeds, randomized
three-block order, quality/thermal gates, independent confirmation counts,
135-Job maximum and two-hour limit. No design choice was changed to obtain a more
favorable outcome. The original sample-size sensitivity and descriptive-analysis
limitations remain applicable; three blocks do not establish method superiority.

The change is the repaired, qualified optimizer runtime. Before fresh F0 or study
creation, verify the actual worker's image and package digest, the mounted
`optimizer_required: true` setting, PyTorch/BoTorch imports, and a bounded actual
qLogNEI calculation. That read-only software check is not experimental evidence
and never enters a policy's observations. A changed image or missing dependency
stops the protocol. Numerical model failures still count as explicit policy
fallbacks; report them and do not replace the run or call fallback a BO acquisition.

Use fresh runtime/model qualification, history and study/attempt IDs. S0 receives
only the new history's nine confirmation profiles, while S1 and S2 start cold.
Neither the predecessor's observations nor the software preflight output enters
the new comparison. S0's first-use budgets again subtract its measured history
acquisition cost, and the final grid runs only after all nine policies are terminal.

The first attempt remains visible: 48 application Jobs and three F0 Jobs cost
140 physical GPU reservation seconds. Report that amount separately and add it
to cumulative project cost; it is not zeroed, hidden or reassigned to a successful
policy. Do not combine those partial studies with new blocks as independent
replication. The fixed repair and predecessor records are linked by content digests
in the new plan.

Regenerate the plan with the same archived inputs:

```sh
uv run python examples/plan_policy_retry.py --output /tmp/new-policy-plan-v2.json
```

Only creation time changes. Generation does not contact the cluster or submit
work. It rejects a live/resumed predecessor, missing optimizer qualification and
unknown/incomplete prior accounting. The original stopped driver has a separate
no-resume guard. All prior records remain unchanged.

For a completed capture, use the existing audit with this plan explicitly:

```sh
uv run python examples/evaluate_policy_comparison.py \
  --capture /private/path/captured-v2.json \
  --plan docs/evidence/policy-comparison-plan-v2.json \
  --output /private/path/new-policy-summary-v2.json
```

Execution and final evaluation remain pending. This registration does not close
the [full v0.3 completion audit](goal-audit.md).
