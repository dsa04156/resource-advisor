# Implementation ledger — design v0.3

The private design document defines this independent repository. Existing
Kubernetes/KubeEdge and Slurm are external compute backends; no other application
API, domain model, source code or database is an implementation dependency.
No private design document or site configuration belongs in this repository.

## Acceptance map

| Milestone | Required evidence | Current evidence |
|---|---|---|
| M0 | Read-only inventory, versions, allowlists, separate backend pools | Read-only Kubernetes/Prometheus inventory and project API verified. Two Slurm hosts have advancing CPU/memory observations through independent mTLS ingestion and console; local controller queue/resource and restricted SSH acceptance now pass; continuous restricted-controller deployment now passes GitOps, fresh persisted API snapshots, project isolation and actual resource/queue console checks; full hardware discovery remains pending (slurm-controller-results.md) |
| M1 | KFP launcher → API → suspended Kubernetes Job → result + MLflow | Actual uncached KFP → API → Kueue → RTX 5080 → S3/MLflow verified, including replay |
| M2 | Slurm submission/status/cancel/results on qualified GPU/NPU | GPU F0/accounting historical proof; native guard and result transport implemented. October 6 controller/worker recovery and corrected Orin GPU admission/denial verified; cgroup OOM observed with a retained parent-state mismatch (slurm-limits-v2-results.md). An isolated Jetson runtime now passes the fixed CNN with native sm_87 and enforced memory, with all earlier failed candidate costs retained (slurm-jetson-runtime-results.md). A subsequent real Slurm CNN producer also passes the platform result/digest/identity contract with 6 GPU reservation seconds, using acceptance-only identities (slurm-cnn-contract-results.md). Compute worker route ownership is scoped and deployed (worker-route-ownership.md). A browser-created native Orin API job now completes through Slurm, validated result, PostgreSQL ledger and byte-identical S3/API/MLflow publication; the failed first attempt remains tracked, totaling 146 GPU reservation seconds. One real console cancellation and bounded project/owner isolation check now pass, with retained 27 GPU reservation seconds and one KILLED MLflow/ledger record. A corrected public verifier now passes the accepted-response loss/SIGKILL protocol with one retained submit call, same Pod restart and native GPU job, one parent/ledger/MLflow and matching S3/API/MLflow bytes. All four trials, including prior invalid verifier trials, retain 290 GPU reservation seconds. Two-project Slurm policy now has separate owners/accounts/forced keys/routes, eight native rejections and both high-first observations. Six completed attempts have matching results/ledger/MLflow and 438 known GPU reservation seconds; final attempt termination/accounting is unresolved after controller transport loss. Full E6/failure scenarios and Pi NPU qualification remain open (slurm-project-isolation.md) |
| M3 | Actual Kueue and Slurm quota/account enforcement, unit-separated ledger | Bounded live quota/priority trials and terminal ledger verified. A single high-grade Slurm API profile now also verifies actual ra-high/priority1000 and result publication through the scoped gateway, with unchanged native limits (slurm-priority-gateway.md). Two-project Kubernetes admission/API/artifact isolation and durable cancellation timing have live evidence; bounded Slurm cross-user gateway checks now pass; direct MLflow/S3 tenant authorization, final two-project attempt termination/accounting and full utilization/energy costs remain open (slurm-project-isolation.md) |
| M4 | Immutable variants, two signatures, consent/budgets, quality-gated profiles | Consent/budget/pilot loop, real GPU confirmation and isolated deterministic JSON-checkpoint training verified; general training and large checkpoint formats remain open |
| M5 | Lookup/random/qLogNEI ask/execute/observe/final-confirmation | Real GPU random/qLogNEI loops verified. Lookup freezes its historical cohort. A fresh preregistered three-block comparison completed with 15 real BO acquisitions and no demonstrated BO selection advantage after the first trial stopped for missing worker optimizer dependencies; prior costs and failures remain visible |
| M6 | Authorization, idempotency, failure/recovery, E0–E7, S0–S2 comparison | Accepted-submit worker crash, running cancellation, completed-before-cancel, terminal-Pod retention, concurrent workers, two-project admission and isolated restore have bounded live evidence. Current seven-table recovery now includes all 25,771 metadata records, 567 result bundles, 582 tracking/usage links and 18 recorded scheduler experiments with unchanged source services (restore-rehearsal.md). S0–S2 comparison and bounded S5 offline replay are published. A bounded six-block B0–B2 operational comparison, source-only rank/numerical workload holdouts with OOD abstention, and the 17-Job E5 kernel/input-supply trial are published. Live E2/E3 abstention, consent/budget and scope rejection now have independent scheduler/DB readbacks. Full Slurm execution/scenario evidence remains open. Numerical intervals remain wide and uncalibrated; expired-lease fencing and node loss are additional unverified operational risks |
| M7 | Shared GPU interference and conditional MF/transfer | Conditional extensions. Actual RGPE/warm-start comparison published without demonstrated selection advantage. MF-KG kernel/qualified-group orchestration implemented; physical fidelity qualification and shared-GPU interference remain open |

## Implementation sequence

M6's [live terminal replay](result-replay.md) additionally verifies exact-result
idempotency, terminal/epoch/attempt rejection and project/operator authorization
through nine actual API requests. Both original Slurm attempts' DB rows, result
artifact and MLflow runs remain unchanged, with no new compute or reservation.
This does not close the disconnected cohort or external lease-fencing risks.
M6's [CUDA fallback gate](cuda-fallback.md) also now has actual positive/negative
Kueue executions, with one correct CUDA result and zero result on explicit GPU
visibility loss. Both terminal Jobs and all incurred costs remain visible.
M6's [Hailo artifact guard](hailo-artifact.md) now rejects an actual different
compiled model before device configuration, with a fresh qualified positive,
unchanged application history and 8 NPU-seconds including the failed arm.

1. Contract validation, stable logical/environment signatures, project authentication.
2. Independent persistence and attempt/outbox state machine with response-loss recovery.
3. Compatibility and measured-history recommendation, immutable approval boundaries.
4. Kubernetes/Kueue and Slurm adapters, result validation and MLflow retry outbox.
5. Consent-bound profiling, separated device accounting and versioned search space.
6. Optional BoTorch qLogNEI with explicit fallback, budget reservation and confirmation.
7. CLI/API, KFP launch/cancel examples, scoped deployment and verification evidence.

Prediction is never a measurement. A successful process is not a validated
result. A configured device is not a validated model runtime. Fake data is
confined to explicitly marked test fixtures. No full-platform completion claim
is possible while required backend/model/quality and scenario gates lack direct
evidence. See the [full acceptance audit](goal-audit.md) for the authoritative
gate-to-evidence map; this milestone summary does not replace it.

## Current site constraints (generalized)

The existing control plane and edge nodes use different Kubernetes versions;
they will not be upgraded. Queue API is v1beta2. Slurm is 24.11.5 and currently
has historical one-GPU Jetson F0, SlurmDBD and QOS enforcement evidence.
The controller and both worker registrations recovered on October 6 after fresh
bidirectional authentication checks and bounded node-state recovery
([evidence](slurm-recovery.md)). A bounded browser/API/model-result/publication path now passes
([evidence](slurm-api-results.md)); full Slurm scenario qualification remains open.
The DEEPX worker still has no detected PCIe accelerator.
Other Hailo hardware executed two classification models whose preregistered
qualifications failed fixed gates; those failures remain unqualified. A third,
separately preregistered [ResNet-50 contract](hailo-resnet50.md) passed and completed
three API observations plus one approved verification with accounting/artifact
delivery. That single contract does not qualify other models or Slurm. Runtime job submission
must remain separate from GitOps. Site endpoints and credentials stay outside
this repository. New data has its own database and artifact namespace.
