# Initial verification — 2026-10-02

This is implementation verification, not a GPU performance evaluation.

| Check | Evidence | What it does not prove |
|---|---|---|
| Contract and failure suite | 42 tests on SQLite; same suite on a dedicated PostgreSQL instance | Live scheduler quota or device isolation |
| PostgreSQL backup/restore | `pg_dump` custom archive restored into a new DB; four table contents compared | Production restore time objectives or failover |
| KFP compilation | SDK 2.13.0, Kubernetes extension 1.5.0; Secret reference, no GPU request, caching disabled | Successful submission to a live KFP server |
| API demo | Three synthetic completed jobs → quality-gated profiles → recommendation → approval → three mocked MLflow runs | Hardware benchmark data or real MLflow delivery |
| Response loss | Accepted submit followed by transport failure; reconcile attaches the original ID; submit count remains one | Every scheduler/network failure |
| Cancellation | Before-submit cancellation avoids remote work; queued cancel waits for confirmation; surviving Pod blocks confirmation | Device cleanup under every node failure |

The suite checks project isolation, immutable records, unapproved resource
changes, stale inventory, stale attempts, digest mismatch, low quality,
production rejection of synthetic data, MLflow outage retry, collection timeout,
and a Pending Pod that must not be reported as Running.

Run from the repository root:

```sh
uv sync --locked --extra pipelines
uv run pytest -q
uv run pytest tests/test_api_flow.py -q -s
uv run python examples/pipeline.py --output /tmp/resource-advisor-pipeline.yaml
uv run ruff check src tests examples
uv run ruff format --check src tests examples
```

For PostgreSQL, set `RA_TEST_DATABASE_URL` to a **dedicated disposable database
whose name starts with `ra_test_`**. Tests reset the service tables in that DB.
Never point this variable at an application database. The local verification
instance is temporary and is removed after checks. Private connection settings,
dump files and site evidence are not committed.

One upstream Starlette warning about its HTTPX test-client integration remains;
it does not fail these checks. The current dependency lock records the tested
versions. GitHub Actions repeats the SQLite and PostgreSQL suite for each push.

Not yet verified at the initial revision: live GPU/NPU benchmark, Slurm QOS enforcement, live Kueue
admission/priority, live KFP→API→GPU execution, MLflow artifact upload, pilot/BO
optimization, workload migration, or production hardening.

## Optimization increment

The suite now has **54 passing tests on both SQLite and isolated PostgreSQL**.
This includes the real BoTorch acquisition implementation and a durable
qLogNEI→synthetic executor→observation→next probe→independent confirmation loop.
It is not a real GPU performance experiment. Numerical GP diagnostics are
retained in the surrogate snapshot instead of being presented as guarantees.

A populated PostgreSQL study, its probe/result records and outbox were backed
up and restored into a new database. Contents of all five service tables,
including `ra_studies`, matched. Test dependency lock: PyTorch 2.8.0+cpu,
BoTorch 0.16.1, GPyTorch 1.15.2. CI installs the optimizer extra so these tests
are executed rather than silently skipped.

The live optimization, budget-overrun/termination behavior on both schedulers,
checkpoint isolation and complete failure accounting are still open gates.

## GPU execution increment

The suite now has **56 passing tests on both SQLite and isolated PostgreSQL**.
Python 3.11 is supported for the qualified GPU runner; CI also checks 3.13.
An actual Kueue-admitted CUDA job executed a 4,096-element vector kernel and
verified every result. See [the sanitized evidence](evidence/cuda-f0.json) and
[the independently implemented probe](../examples/hardware_probe.cu).

A pinned base image plus an operator-qualified package PVC and immutable source
ConfigMap also ran PyTorch 2.8.0+cu128 on a physical RTX 5080. These mounts are
read-only in workload Pods, with a checked environment signature. This is lab
qualification: the PVC is not a content-addressed, automatically attested
production image. Runtime-bundle configuration belongs to the operator, never
to the job submitter. The worker credential is namespace-scoped and cannot read
Secrets or create jobs in other namespaces.

Live confirmation exposed a time-budget defect: a maximum run-time allowance
left only one second for queueing. Planning now reserves queue time before
allocating run time, within the same per-confirmation wall allowance. A
regression test checks that both queueing and execution receive useful time
without consuming another confirmation's reservation. This split is a bounded
heuristic, not a workload duration prediction or a guarantee of admission.

## Live tracking and study increment

Random search and qLogNEI completed real GPU exploration and independent
confirmation. Both retained the baseline. Lookup abstained on a real queue
deadline. Full initial and corrected rounds are retained in the
[hardware report](gpu-experiment.md); this is not a claim of optimization gains.

A real MLflow server received 28 hardware runs in an explicitly mapped dedicated
experiment. All corresponding outbox events are DONE and independent DB run links
exist. Parameters, measured metrics, quality/signature tags and terminal run state
were read back. Artifacts, failed-job tracking and MLflow authorization enforcement
remain open. Response-loss tests verify reuse of the external run and timestamp.

The current suite passes **58 tests on SQLite and 58 on isolated PostgreSQL**;
lint and format checks pass. The live database containing the GPU studies and
tracking links was also dumped and restored into a fresh DB: all five service
tables matched by count and deterministic content hash. The lab database uses
ephemeral storage; a private backup was retained. This is recovery verification,
not persistent production deployment.

## Result-artifact increment

The suite passes **68 tests on SQLite and 68 on isolated PostgreSQL**, with the
artifacts extra installed. 28 real GPU result bundles passed conditional S3 upload,
byte/digest verification, authenticated API download and MLflow artifact upload/list/read
checks. A disconnected storage connection returned 503 while preserving compute
success, then recovered on reconnect. See [artifact evidence and limitations](artifacts.md).

## Slurm policy increment

The suite passes **73 tests on SQLite and 73 on isolated PostgreSQL**. New
priority checks cover backend-specific policy mapping and refusal of an unmapped
high grade, with no direct numeric priority override or Pod preemption priority.
The previous UTC accounting regression and driver-only F0 are retained.

Live Slurm 24.11.5 now enforces the dedicated account's resource/QOS limits.
Four invalid requests were rejected, simultaneous work waited for the account CPU
limit, and a later high-QOS GPU job started before an older normal-QOS GPU job.
Both completed the CUDA probe. Earlier verifier failures and cancelled allocations
are retained alongside the successful trial in [the policy report](slurm-policy.md).
`sprio` factor lookup failed; the report uses actual `scontrol` priority and `sacct`
start order rather than inventing a factor breakdown. This does not complete the
Compute API → Slurm model/result path or cross-user isolation testing.

## Live Kueue policy increment

The existing one-GPU lab queue kept a two-GPU Job suspended without creating a Pod.
With the GPU reserved, normal and high jobs both waited on unused-quota conditions.
Releasing the holder admitted the later high job first; both jobs then verified
4,096 real CUDA output values. Their conditions, start/completion times and cleanup
state are retained in [the Kueue policy report](kueue-policy.md). Kubernetes remained
v1.31.14 and Kueue v0.19.5; no runtime upgrade or preemption change was made.
This is direct scheduler acceptance evidence, not a completed KFP/API integration.

## Terminal accounting increment

The full suite passes **80 tests on SQLite and isolated PostgreSQL**. Accounting
records now commit atomically with every terminal attempt, and summaries expose
unknown/legacy data without merging accelerator units. A real API-submitted GPU
benchmark with an injected collector outage produced RESULT_INVALID and 2 observed
GPU allocation seconds without a profile; pre-submit cancellation recorded zero.
Four missing terminal records were backfilled idempotently. Read-only Slurm checks
recovered actual failed/cancelled AllocTRES after fixing purged-ID lookup. The live
database was backed up and restored with all five table contents matching.
See [accounting evidence and its remaining gaps](accounting.md).

## Live Kubeflow increment

The suite passes **81 tests on SQLite and isolated PostgreSQL**. Direct TLS
serving requires both key and certificate; the pipeline trusts an explicit CA
bundle from a namespace Secret and sends the project token without embedding it
in workflow parameters. An unprivileged, digest-pinned launcher image was built
with hash-checked locked dependencies and verified in an actual Pod.

The uncached KFP → API → Kueue → RTX 5080 → validated result chain succeeded.
S3/API byte readback, a FINISHED MLflow run and its artifact were verified.
An earlier disk-pressure eviction is retained as a failed infrastructure attempt.
See [the workflow report and measured limitations](kubeflow-pipeline.md).
