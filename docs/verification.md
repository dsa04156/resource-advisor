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

Not yet verified: live GPU/NPU benchmark, Slurm QOS enforcement, live Kueue
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
