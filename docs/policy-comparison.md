# Repeated GPU policy comparison

The [prospective protocol](policy-comparison-plan.md) was stopped after the first
BO study exposed missing optimizer dependencies in the worker image. The
[launch evidence](evidence/policy-launch.json) remains a dated historical snapshot.
The [stopped-run evidence](evidence/policy-stop.json) supersedes its running status.
This is not the completed nine-study comparison or a policy effectiveness claim.

The image inherited `artifacts` dependencies without the `optimizer` extra.
Actual GPU benchmark containers had their qualified CUDA/PyTorch runtime, but
the CPU service responsible for choosing configurations could not import torch.
The study recorded three initial-design choices followed by five explicit
`MODEL_FAILURE_RANDOM_FALLBACK` choices; it never executed a qLogNEI acquisition.
This is an infrastructure failure, not evidence about BO's effectiveness.

The experiment driver was stopped and the active study canceled through the API.
It became terminal with 13 completed observations; none was replayed. History,
random search and lookup completed 15, 14 and six Jobs respectively. All 48
application results were checked against their unique ledger/MLflow records and
identical S3/API/MLflow artifact bytes. Three F0 Jobs remain separate. No later
block or post-hoc oracle was submitted. The stopped protocol must not resume with
a changed image or be silently replaced; a new run needs a new preregistration.

The deployment guard now supports `optimizer_required: true`, which rejects
missing PyTorch/BoTorch before worker backend or database construction. The lab
worker has been repaired through CI-gated manual Argo sync, using the qualified
optimizer runtime and the current source. Its mounted configuration requires the
optimizer; the running package hash matches the image build. A bounded read-only
calculation on the first three retained observations returned actual
`CONSTRAINED_QLOGNEI` output in 3.35 seconds (PyTorch 2.8.0+cpu, BoTorch 0.16.1).
The same guard rejected the preceding artifact-only image with exit 2. Database
fingerprints, other core processes, Secrets/PVC and compute ownership were
preserved. See [repair evidence](evidence/optimizer-repair.json). This calculation
submitted no GPU Jobs, created no study observations and does not complete or
retroactively repair the stopped comparison. A [new prospective trial](policy-comparison-v2.md)
is registered separately; its execution and evaluation remain pending.

API and worker now run the source of `ca253134ab96b5486b53bf66a493982b3bb484b3`.
Their running package hashes match the source-only image build. Four core Argo
Applications are pinned to that commit and were verified Synced/Healthy after
manual sync. The nine existing static resource identities, inventory/database
processes, historical compute ownership and database fingerprints were preserved.
The updated worker configuration adds qualified runtime bindings; it does not
expand cluster quotas. During API Pod replacement the first external health read
encountered TLS EOF; the managed forwarding service recovered and certificate-
verified health, anonymous denial and the new lookup request schema passed.

Three fresh CUDA F0 Jobs passed numerical agreement and execution-bound thermal
eligibility with CPU requests 0.5/1/2, each reserving one physical GPU. The same
immutable source ConfigMap, generated model/input, runtime and memory limits are
used by the comparison. There is no new hardware qualification inferred from an
old benchmark or from unit tests.

The initial history grid completed 15 actual Jobs: six probes and nine independent
confirmations. It cost 373.906167 study wall seconds and 43 physical GPU reservation
seconds. All 15 results were checked against Kubernetes/Kueue, PostgreSQL accounting,
S3, the HTTPS API and unique MLflow runs, including identical artifact bytes.

S0's separate immutable workload registration has the **same WorkloadSignature,
contexts and quality contract**, but reduces its per-study ceilings to 826 wall
seconds and 857 GPU seconds. These are the original limits less the measured
history acquisition cost, with wall time rounded down. Its runtime variants use
the same qualified executable and environment. This makes the first-use cap an
execution constraint, not merely a reporting adjustment. All three S0 studies
receive the same nine profile IDs; later results cannot enter their selection.
The report charges history once across the three actual reuses and also shows
each policy's first-use boundary explicitly.

The driver initially confused `COMPLETED` observation outcomes with `SUCCEEDED`
backend Job states and stopped after history collection. The original study was
read back as terminal with all 15 successful observations. Corrected orchestration
resumed that handle; no history Job was replayed or replaced. This driver issue is
separate from the successful hardware outcomes and is retained in the record.

## Offline audit

`examples/evaluate_policy_comparison.py` reads a complete captured protocol without
submitting Jobs or fitting another model:

```sh
uv run python examples/evaluate_policy_comparison.py \
  --capture /private/path/captured.json \
  --plan docs/evidence/policy-comparison-plan.json \
  --output /private/path/new-summary.json
```

It rejects missing/overlapping attempts, changed workload/resource conditions,
unbalanced or infeasible history, altered first-use budgets, unknown GPU cost,
future/oracle leakage, confirmation reuse, incomplete independent validation,
early oracle measurements and whole-protocol deadline overruns. It reports each
block's selection, finite-oracle regret, raw paired differences, accounting costs,
unknown queue intervals and actual model-based choices. The later finite oracle
is an estimate under potentially different load, not ground truth.

Synthetic negative tests validate these audit rules. The stopped capture correctly
fails the complete-protocol audit. All nine valid comparisons and post-hoc oracle
verification remain pending; the full [v0.3 audit](goal-audit.md) stays open.
