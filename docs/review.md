# Engineering review and remaining acceptance gates

This review records scope and remaining gaps, not production certification.
The first-implementation fixes below are historical. Current claims are bounded
by their linked evidence and the [full completion audit](goal-audit.md).

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
independent confirmation, including real GPU runs. A completed three-block
[S0/S1/S2 comparison](policy-comparison-v2.md) now retains the stopped predecessor
and full measured allocation costs without demonstrating BO superiority.
Complete phase/utilization accounting, broader B0–B2 operational effectiveness
and unseen-family numerical uncertainty remain evaluation gates; a bounded source-only rank holdout is now published.
The small smoke experiment does not prove BO superiority or hard physical budget bounds.
The first preregistered comparison exposed a shallow deployment integration:
optimizer tests passed with local dependencies, but the deployed worker lacked
them and made random fallback choices. That trial stopped and retained its costs.
The [repaired worker and new trial](policy-comparison-v2.md) bind the actual image,
require optimizer dependencies at startup and verify a real model calculation
before new GPU work. CI with optimizer packages alone is not deployment proof.
Recommendation reuse now rechecks source age/scope and post-recommendation
residuals before approval and submission. A historical chronological audit found
both eligible later GPU results outside their saved posterior intervals. The
heuristic drift gate and this two-point report are not calibrated uncertainty;
numerical workload holdout and interval calibration remain open. The later
[bounded live drift trial](load-drift.md) verifies latching and stale-approval
rejection; its [completion-state follow-up](kubernetes-completion.md) fixes a
transient requeue observation and the resulting false queue-timeout risk.

**Artifacts and environments:** bounded JSON result bundles now have conditional
S3 writes, verified API downloads and live MLflow artifact copies. Large-model
transfer, storage retention/backup, image attestation, general checkpoint isolation and
runtime qualification on each GPU/NPU are still required.
The bounded deterministic training fixture now has real GPU isolation evidence;
this does not qualify arbitrary training programs or larger checkpoint formats.

**Accounting:** terminal outcomes now have an atomic, unit-separated ledger,
including collector failures, pre-submit cancellation and preflight rejection.
Unknown allocation remains null. [Durable cancellation timing](termination-retention.md)
now has actual worker-interruption and completed-before-cancel evidence using
retained kubelet termination records. Complete preparation, utilization, energy
costs and disconnected-node accounting remain open.

**MLflow and observability:** real hardware results were delivered to a
dedicated experiment, with project-to-experiment routing and durable run links.
Result-bundle artifact upload and the [four-view console](console.md) are verified.
The console also consumes actual [Slurm host telemetry](slurm-inventory.md),
while controller-dependent queue/reservation values remain unknown.
Server-side multi-tenant authorization, circuit breakers and comprehensive event
metrics remain open. Experiment routing
alone is not an MLflow authorization boundary.
Terminal tracking now includes resultless failures/cancellations atomically with
the usage ledger. Six historical live attempts recovered through real MLflow,
including connection refusal and a discarded accepted-create response. Invalid
results cannot be published as valid performance metrics. Unknown legacy end
times remain unknown. These checks do not prove global exactly-once creation.

**Deployment:** the ephemeral lab DB has been replaced by same-version PostgreSQL
on a PVC with a restricted app role. All five tables matched before/after new-Pod
recreation, and 46 existing artifact objects matched restored metadata. The source
and private backup were retained. This establishes Pod-replacement persistence,
not node-loss recovery, HA, schema migration or key rotation. A later
[independent online restore rehearsal](restore-rehearsal.md) verified exact
metadata and artifact/tracking links while source writers stayed running; it
still does not establish off-site or whole-object-store recovery.
CI uses disposable databases; no real cluster access is required.
The API, inventory and single Kubernetes worker now run as scoped Deployments.
An actual worker SIGKILL between GPU Job acceptance and external-ID persistence
recovered without another create call; Job/Pod UIDs, ledger and MLflow identities
were retained. This closes that one crash window. A separate [two-worker trial](concurrent-workers.md)
also verifies one actual Job/Pod/ledger/MLflow run under concurrent execution;
it does not prove expired-lease external-service fencing, Slurm recovery, node
loss or production ingress. [Pinned manual-sync Argo applications](gitops-adoption.md)
manage static services, leaving compute Jobs outside GitOps. The namespace Job-creation role
also remains a trusted controller permission, not hostile-tenant sandboxing.

**Hardware:** an unresolved NPU PCIe link blocks model/runtime qualification on
that device. It is not registered as an executable candidate based only on an
installed driver or device plugin.
Separate Hailo hardware did execute two classification models, but
[both qualifications failed a preregistered quality gate](hailo-efficientformer.md).
Their measured failures and costs are retained; neither has been converted into
a successful model qualification or claimed platform NPU execution.
A third independently preregistered [ResNet-50 trial](hailo-resnet50.md) passed
80% accuracy and 97% reference agreement, then completed the bounded four-Job
API/approval/accounting/S3/MLflow path. The same input subset is repeated across
Jobs; NPU memory/utilization/power remain unknown. Its later verification fell
outside the lookup mean interval. This supports one execution contract, not
cross-device optimization or calibrated performance prediction.

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

The bounded B0/B1/B2 GPU operational trial is now published in
[operational-comparison.md](operational-comparison.md): approximately 1% shorter
compute did not recover profiling cost over six actual uses. Collector recovery
remains visible; this does not establish fleet or cross-backend benefit.
