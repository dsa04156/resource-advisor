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

**Live execution:** scoped Kubernetes/Kueue CUDA and PyTorch jobs now have actual
hardware evidence, including actual uncached KFP launch and replay. Historical
Slurm CUDA/accounting/QOS trials passed, but its full service/model path remains
open and the controller is currently unreachable. Disconnection recovery and
complete cancellation/cost evidence remain required.

**Optimization:** consented lab pilot/BO execution has durable reservation and
independent confirmation, including real GPU runs. Complete failed-run cost
accounting and randomized equal-budget trials remain production/evaluation gates.
The small smoke experiment does not prove BO superiority or hard physical budget bounds.

**Artifacts and environments:** bounded JSON result bundles now have conditional
S3 writes, verified API downloads and live MLflow artifact copies. Large-model
transfer, storage retention/backup, image attestation, general checkpoint isolation and
runtime qualification on each GPU/NPU are still required.
The bounded deterministic training fixture now has real GPU isolation evidence;
this does not qualify arbitrary training programs or larger checkpoint formats.

**Accounting:** terminal outcomes now have an atomic, unit-separated ledger,
including collector failures, pre-submit cancellation and preflight rejection.
Unknown allocation remains null. Complete preparation, utilization and energy
costs and durable cancellation timing remain open.

**MLflow and observability:** real hardware results were delivered to a
dedicated experiment, with project-to-experiment routing and durable run links.
Result-bundle artifact upload is verified. Server-side multi-tenant authorization, circuit breakers,
comprehensive event metrics and dashboards remain to implement. Experiment routing
alone is not an MLflow authorization boundary.
Terminal tracking now includes resultless failures/cancellations atomically with
the usage ledger. Six historical live attempts recovered through real MLflow,
including connection refusal and a discarded accepted-create response. Invalid
results cannot be published as valid performance metrics. Unknown legacy end
times remain unknown. These checks do not prove global exactly-once creation.

**Deployment:** real PostgreSQL tests and a restore check pass. This is not an
HA deployment, migration strategy, key rotation system or workload isolation
audit. CI uses disposable databases; no real cluster access is required.

**Hardware:** an unresolved NPU PCIe link blocks model/runtime qualification on
that device. It is not registered as an executable candidate based only on an
installed driver or device plugin.

## Execution-boundary fixes

- Slurm previously ignored a declared container image. It now rejects that
  unsupported execution mode before submission; native execution requires an
  explicit qualified binding and file/probe guard.
- Node-local logs no longer require an assumed shared filesystem. Reads require
  matching accounting ownership and an operator-selected SSH alias.
- Pure configuration rejection no longer becomes uncertain remote submission:
  preflight runs before intent and records zero allocation on rejection.
- Same-name jobs in another Slurm account cannot satisfy response-loss recovery.

These fixes have regression tests and real ARM guard evidence. They do not prove
an end-to-end Slurm model execution while its controller is unavailable.
