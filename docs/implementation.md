# Implementation ledger — design v0.3

The private design document defines this independent repository. Existing
Kubernetes/KubeEdge and Slurm are external compute backends; no other application
API, domain model, source code or database is an implementation dependency.
No private design document or site configuration belongs in this repository.

## Acceptance map

| Milestone | Required evidence | Current evidence |
|---|---|---|
| M0 | Read-only inventory, versions, allowlists, separate backend pools | Read-only Kubernetes/Prometheus inventory and project API verified. Two Slurm hosts have advancing CPU/memory observations through independent mTLS ingestion and console; controller-backed inventory and full hardware discovery remain pending |
| M1 | KFP launcher → API → suspended Kubernetes Job → result + MLflow | Actual uncached KFP → API → Kueue → RTX 5080 → S3/MLflow verified, including replay |
| M2 | Slurm submission/status/cancel/results on qualified GPU/NPU | GPU F0/accounting historical proof; native guard and result transport implemented; controller unreachable, full model/API path pending |
| M3 | Actual Kueue and Slurm quota/account enforcement, unit-separated ledger | Bounded live quota/priority trials and terminal ledger verified. Two-project Kubernetes admission/API/artifact isolation and durable cancellation timing have live evidence; direct MLflow/S3 tenant authorization, Slurm cross-user gates and full utilization/energy costs remain open |
| M4 | Immutable variants, two signatures, consent/budgets, quality-gated profiles | Consent/budget/pilot loop, real GPU confirmation and isolated deterministic JSON-checkpoint training verified; general training and large checkpoint formats remain open |
| M5 | Lookup/random/qLogNEI ask/execute/observe/final-confirmation | Real GPU random/qLogNEI loops verified. Lookup freezes its historical cohort. A fresh preregistered three-block comparison completed with 15 real BO acquisitions and no demonstrated BO selection advantage after the first trial stopped for missing worker optimizer dependencies; prior costs and failures remain visible |
| M6 | Authorization, idempotency, failure/recovery, E0–E7, S0–S2 comparison | Accepted-submit worker crash, running cancellation, completed-before-cancel, terminal-Pod retention, concurrent workers, two-project admission and isolated restore have bounded live evidence. S0–S2 comparison and bounded S5 offline replay are published. Complete E0–E7, B0–B2 operational comparison and workload holdout remain open; expired-lease fencing and node loss are additional unverified operational risks |
| M7 | Shared GPU interference and conditional MF/transfer | Conditional extensions. Actual RGPE/warm-start comparison published without demonstrated selection advantage. MF-KG kernel/qualified-group orchestration implemented; physical fidelity qualification and shared-GPU interference remain open |

## Implementation sequence

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
The controller is currently unreachable, so its current scheduler health and
complete model/API path are not asserted. The DEEPX worker has a PCIe link failure.
Other Hailo hardware did execute classification models, but both preregistered
model qualifications failed a fixed quality gate; those failed results do not
authorize platform execution. Runtime job submission
must remain separate from GitOps. Site endpoints and credentials stay outside
this repository. New data has its own database and artifact namespace.
