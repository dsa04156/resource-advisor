# E0–E7: design-to-evidence matrix

This is the acceptance map for independent design v0.3, last edited October 2,
2026. It links previously retained live reports; assembling the map does not
rerun experiments or extend their scope. PASS below applies only to the named
bounded scenario. Unsupported devices and incomplete failure boundaries remain
explicit. Optional shared-GPU E8/M7 is separate.

| Scenario | Required behavior | Evidence and bounded status | Remaining boundary |
|---|---|---|---|
| E0 accelerator correctness | Actual accelerator execution, output checks, fallback/incompatible-runtime negatives | **PARTIAL.** [Kubernetes GPU](gpu-experiment.md), [qualified Hailo model](hailo-resnet50.md), [Orin runtime](slurm-jetson-runtime-results.md), [unreserved CUDA denial](slurm-limits-v2-results.md), [architecture/precision denial](contract-boundaries.md), and [actual CUDA fallback refusal](cuda-fallback.md), and [different Hailo HEF rejection](hailo-artifact.md) | Hardware/model-specific support only; Pi NPU absent from PCIe. Explicit CUDA fallback refusal and this Hailo compiled-artifact binding guard are verified; hostile-user isolation and other runtime/model conversion cases remain separate |
| E1 common execution contract | Each backend submits, reports state, cancels, collects validated results and prevents duplicate submission | **PASS for qualified fixtures.** [KFP/Kueue GPU](kubeflow-pipeline.md), [Kubernetes cancellation](cancellation-verification.md), [Slurm API result](slurm-api-results.md), [Slurm cancellation](slurm-cancellation.md), [same-attempt response-loss recovery](slurm-response-recovery.md) | Slurm CNN, Kubernetes CUDA and Hailo are separate RuntimeVariants, not an identical CUDA image on all hardware. New controller outage is still unresolved |
| E2 unknown work / consent / budgets | No-history abstention, no unconsented pilot, explicit budget/reserved confirmation | **PASS for bounded Kubernetes API cases.** [Live contract trial](contract-boundaries.md) preserves zero new API execution during negatives, with an independently measured history positive control and separately charged fresh qualification | Arbitrary researcher programs require a cooperative qualified entrypoint; no first-run optimal-resource prediction is claimed |
| E3 history scope | Input shape, batch and runtime changes create separate scope; invalid old profiles are excluded | **PASS for bounded descriptor negatives.** [Live contract trial](contract-boundaries.md), [history freezing](lookup-history.md), [load drift](load-drift.md) | Changed programs were rejected, not successfully executed/qualified; broad runtime equivalence is unproven |
| E4 operational benefit | Valid fixed baseline versus measured/approved recommendation; quality, completion/failure and profiling-inclusive cost | **PASS as a bounded comparison, with null net benefit.** [B0/B1/B2](operational-comparison.md): 35 real GPU Jobs, six main uses per arm, no profiling cost recovery; [S0/S1/S2](policy-comparison-v2.md) makes no BO superiority claim | One generated workload/GPU cannot establish fleet effectiveness or trained-model quality; separate NPU/Slurm performance must not be treated as scheduler superiority |
| E5 bottleneck evidence | Separate input-supply and GPU-compute conditions, evidence-based diagnosis without automatic GPU shrink | **PASS for fixed CUDA fixture.** [Kernel comparison](e5-kernel-results.md): 17 Jobs with predecessors retained, profiler/plain timing separated and GPU count unchanged | Real disk/network, asynchronous loaders and broad training cases unverified |
| E6 shared-use policy | Two projects' normal, oversized and high-priority requests; queue/order/limits and unique usage | **PARTIAL overall.** [Kubernetes two-project acceptance](project-isolation.md) passes. [Slurm two-project trial](slurm-project-isolation.md) has independent owners/accounts, eight native rejections, live foreign-owner denial and both high-first observations | Last Slurm normal attempt remains CANCEL_REQUESTED after transport loss; final native outcome/cost/outbox and end-state preservation remain open. Six-result Slurm acceptance did not pass |
| E7 failure consistency | Test workload termination, accepted-response loss, collector/MLflow interruption, late-result rejection and idempotent replay | **PARTIAL overall; boundaries below.** Actual worker crashes, cancellation, tracking recovery and independent metadata restore have separate records | A unit/state-machine regression is not live external-service fencing. Current disconnected Slurm final attempt remains unresolved |

## E7 boundaries

| Boundary | Observed evidence | Limit |
|---|---|---|
| Running test allocation termination | [Kubernetes](cancellation-verification.md), [retained termination evidence](termination-retention.md), [Slurm](slurm-cancellation.md), [KFP cancellation](kubeflow-cancellation.md) | Slurm cancel occurred during verification, not useful CUDA kernel execution |
| Accepted submit response lost, worker killed | [Kubernetes actual SIGKILL](worker-recovery.md), [Slurm actual SIGKILL](slurm-response-recovery.md) | Same IDs and one retained submit invocation verified for those trials; not every lease-expiry/partition boundary |
| Collector interruption and reprocessing | [B0/B1/B2 retained collector interruption](operational-comparison.md), [concurrent-worker live trial](concurrent-workers.md) | Both recorded and observed completion times remain visible; no replacement compute |
| MLflow unreachable / accepted-create response lost | [Real MLflow delivery recovery](failure-tracking.md): six original terminal attempts, one run each, unchanged compute records | Client connection refusal and discarded real response were injected; server-wide outage, tenant isolation and distributed exactly-once not certified |
| Actual MLflow Deployment stop/restore | [Retained server interruption](mlflow-server-outage.md): original failure has one ledger and one FAILED run after restoration | **INCOMPLETE.** New variant lost its mounted runtime and failed before importing the runner; 1 GPU-second retained. Reporting-tool errors corrected; [explicit runtime-bundle binding](kubernetes-runtime-bundles.md) is deployed and a separate normal-service GPU qualification passed with2GPU-seconds. That successful Job does not replace or pass the original outage trial |
| Late/stale response protection | [Reproduced regressions and concurrent PostgreSQL insertion](concurrent-workers.md), [nine real API replay/rejection requests](result-replay.md) | Live terminal/epoch/attempt guards and unchanged DB/artifact/MLflow verified on retained Slurm attempts; independent delayed collector and expired-lease external-service fencing remain unverified |
| Metadata backup and reconnect | [Separate current-schema restore](restore-rehearsal.md): seven tables/25,771 records, 567 bundles, 582 run/usage links, 18 scheduler histories | Existing S3/MLflow references reconnected; their services/data were not separately restored |
| Unexpected live Slurm connectivity loss | [Retained two-project trial](slurm-project-isolation.md) | Cancellation is requested, not confirmed. Preserve SAME final ID and unknown cost; do not fabricate a completed ledger or restart the cohort |

## Completion rule

The [milestone ledger](implementation.md) and [full audit](goal-audit.md) retain
required M0–M6 gates. The matrix itself is now documented; it is not proof that
every row passed. R1 and S0/S1/S2 have linked physical execution/comparison;
conditional fidelity/transfer groups retain their qualification and limits.
Physical Pi accelerator execution, final Slurm cohort reconciliation and
remaining required failure/compatibility cases still prevent full completion.

The earlier, broader HAIRP request additionally includes Edge runtime replanning,
HAMi interference, eBPF and other workflows. Its
[separate scope map](hairp-original-scope-ko.md) must not be replaced by this
independent v0.3 acceptance matrix.
