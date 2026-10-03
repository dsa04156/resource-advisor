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

The new trial is now executing. Its [dated launch evidence](evidence/policy-v2-launch.json)
records a fresh actual-worker configuration/model preflight, three successful
GPU F0 checks and 15 completed history Jobs verified against Kueue, PostgreSQL,
S3, API and MLflow. Only six qualified runtime bindings were added. Reloading the
idle worker preserved its image, static resource specs, other core processes and
database fingerprints; the four core Argo Applications remained Synced/Healthy.

The offline audit now requires the predecessor's exact content digest and measured
cost when a plan declares a predecessor. It reports each policy's first-use/reuse
costs separately from prior failed-trial overhead, and adds both trials to the
cumulative project total. It rejects attempts recorded before their own study's
creation and refuses to summarize this partial capture as complete.

The nine policy comparisons and final oracle evaluation remain in progress or
pending. This launch does not close the [full v0.3 completion audit](goal-audit.md).

The [figure generator](../examples/plot_policy_comparison.py) reruns the same
audit before rendering. It rejects a partial capture or changed plan. Once the
capture is complete, create a new output directory:

```sh
uv run --isolated --no-project --with matplotlib==3.10.7 \
  python examples/plot_policy_comparison.py \
  --capture /private/path/captured-v2.json \
  --plan docs/evidence/policy-comparison-plan-v2.json \
  --output /private/path/new-policy-figure-v2
```

The PNG/SVG show raw independent confirmation runs, first-use GPU reservation
cost, actual three-use cost with history charged once, and the later reference
runs. CSVs and a provenance manifest accompany the figure. Short horizontal
marks denote arithmetic means, not confidence intervals; the full result capture
retains every option and failed/abstaining outcome. The selected-configuration
panel alone cannot establish that one algorithm is faster. Duration is the sum of
12 measured blocks with 32 CUDA forwards each, including preprocessing and
transfer; it is not per-request latency. Prior stopped-trial, F0 and oracle costs
remain in the report outside the individual policy bars. No graph is published
from an incomplete experiment.
