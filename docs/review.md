# Initial engineering review

This review records scope and remaining gaps, not production certification.

## Fixed during the first implementation

- An accepted scheduler submission followed by a lost response could be
  duplicated. Persisted intent and external attempt reconciliation now prevent
  blind resubmission; tests verify the remote submit count remains one.
- A Job can disappear before its Pods finish terminating. Cancellation now
  waits for remaining nonterminal Pods before reporting CANCELED.
- Kubernetes Job `active` includes Pending Pods. Running state now requires the
  workload container to be running; allocation timing uses Pod scheduling and
  container termination instead of equating Job admission with execution.
- Cancellation before any submission does not need a remote operation; it is
  distinguished from uncertain in-flight submission.
- A failing outbox event could repeatedly block other work. Retry backoff now
  permits another pending event to proceed.
- A variant could otherwise be reused with a different logical code/data
  contract. It now binds the complete workload signature, in addition to the
  model, shape, precision and environment checks.
- Collection could remain pending indefinitely. A bounded collection deadline
  now makes missing/invalid results visible without inventing success.

## Open implementation gates

**Live execution:** generated manifests/scripts and test doubles are not live
Kubernetes/Slurm evidence. Qualify the service with real scoped GPU jobs,
Kueue admission, cancellation, disconnection and Slurm accounting tests.

**Optimization:** do not activate pilot/BO endpoints before durable budget
reservation, failure-cost accounting, independent final confirmation, runtime
contracts and randomized equal-budget comparisons exist. Optional optimizer
dependencies and schema fields do not count as an integration.

**Artifacts and environments:** JSON digest verification is implemented; model
artifact transfer, image attestation, checkpoint isolation and runtime
qualification on each GPU/NPU are still required.

**Accounting:** collected outcomes have a unit-separated ledger. Reconcile
backend-only failures and cancellation into accounting before reporting complete
project costs. Do not claim Slurm quota enforcement without accounting/QOS
configuration and an actual rejection test.

**MLflow and observability:** metadata delivery has a durable retry queue;
artifact upload, actual server validation, project segregation, circuit
breakers, comprehensive event metrics and dashboards remain to implement.

**Deployment:** real PostgreSQL tests and a restore check pass. This is not an
HA deployment, migration strategy, key rotation system or workload isolation
audit. CI uses disposable databases; no real cluster access is required.

**Hardware:** an unresolved NPU PCIe link blocks model/runtime qualification on
that device. It is not registered as an executable candidate based only on an
installed driver or device plugin.
