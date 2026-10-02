# Track failed and cancelled attempts without fabricated results

Every durable terminal ComputeAttempt now queues MLflow metadata in the same SQL
transaction as its terminal state and usage row. A missing or invalid result must
not make a failed experiment disappear from the experiment history.

| Platform state | MLflow status | Measurements |
| --- | --- | --- |
| SUCCEEDED | FINISHED | Validated result fields only; quality gate remains a separate tag |
| FAILED | FAILED | Only if a validated result exists |
| RESULT_INVALID | FAILED | None; an invalid envelope is audit evidence only |
| CANCELED | KILLED | None when no validated result exists |
| REJECTED terminal attempt | FAILED | None without a validated result |

Requests rejected before a ComputeAttempt exists do not create an experiment run.
Queue deadlines, preflight failures, cancellation before submission and collection
deadlines all use the same terminal path. An MLflow outage cannot roll back or
change a completed compute outcome. No missing latency, memory, quality or GPU
utilization is replaced with zero. No profile or model artifact is invented.

## Delivery and recovery

Outbox IDs remain `mlflow-<attempt_id>`. The delivery worker reads the authoritative
terminal job and optional result from SQL; historical outboxes containing a copied
result payload remain compatible. It records project, job, attempt, signatures,
platform state, result presence/validity, failure code and execution mode. The
declared runtime/resource configuration is retained as parameters.

The worker searches the explicitly mapped MLflow experiment by attempt ID before
creating a run. An existing run must match experiment, project, job and attempt;
foreign or duplicate matches are retained as delivery errors rather than modified.
It saves the external run ID before logging metrics and finalizing status. Retry
uses the original recorded timestamps. Missing legacy terminal timestamps are
tagged `resource_advisor.end_time_source=unknown_legacy`, and the update omits
`end_time`; it never substitutes recovery time for execution completion time.

Recorded `end_time` otherwise represents the platform's terminal observation,
not pure GPU compute completion. Start time uses the observed allocation start,
or request creation when no allocation start exists. Use the separate allocation
ledger for accounting; MLflow's displayed run duration is not GPU compute time.

Backfill only missing outbox IDs, without rewriting jobs, results or usage:

```sh
# RA_DATABASE_URL is provided by the operator's private environment.
uv run resource-advisor backfill-tracking
```

The normal worker delivers the queue. For a bounded metadata-only recovery without
submitting workloads, use the existing delivery class with an explicit project map:

```python
import os
from resource_advisor.store import Store
from resource_advisor.worker import MLflowDelivery

store = Store(os.environ["RA_DATABASE_URL"])
delivery = MLflowDelivery(
    store,
    os.environ["RA_MLFLOW_URL"],
    experiments={"your-project": os.environ["RA_MLFLOW_EXPERIMENT_ID"]},
    token=os.environ.get("RA_MLFLOW_TOKEN"),
)
try:
    for _ in range(100):
        if not delivery.deliver_one():
            break
finally:
    delivery.client.close()
```

`deliver_one()` means an event was attempted, not necessarily delivered. Inspect
the outbox's `status`, `tries`, `last_error` and `lease_until`. Failed delivery keeps
the event pending with backoff; retry when eligible. Do not clear immutable run
links or submit the computation again to repair tracking.

## Verified on the live lab

The existing dedicated MLflow **3.16.1** server received six previously missing
terminal attempts: five cancellations and one result-collection deadline failure.
These are real historical application records, not newly manufactured GPU faults.
No new GPU workload was launched for this recovery.

- A real connection refusal affected only the delivery client's reserved local
  non-listening port. The event stayed pending and later recovered.
- A fault-injection HTTP transport forwarded a create request to the real server,
  discarded its successful response, and raised a read timeout. Retrying found
  that server-created run and reused it.
- Independent REST searches found exactly one run for each of the six attempts.
  Five were `KILLED`, one was `FAILED`, and none contained measurement metrics.
- Four older cancellations had no terminal timestamp. The live MLflow runs kept
  `end_time` absent and recorded the unknown-time tag.
- Before/after comparisons confirmed all job rows, result entities and usage rows
  were unchanged. Repeated backfill enqueued zero additional events.

See [raw verification](evidence/failure-tracking.json). Tests also cover atomic
rollback, corrupted result exclusion, immutable source records, create-response
loss, project/job/attempt/experiment mismatches and duplicate search matches.

This is not a proof of globally exactly-once delivery under arbitrary partitions
or expired worker leases. MLflow does not provide a unique attempt-ID constraint;
ambiguous matches require reconciliation. Server-side tenant authorization,
model registration, resultless failure artifacts and full runtime crash scenarios
remain separate gates. Existing successful run history is not rewritten by this
backfill.

Protocol reference: [MLflow REST API](https://mlflow.org/docs/latest/api_reference/rest-api.html).
Compatibility here was tested against the actual 3.16.1 server, not inferred from
the latest documentation alone.
