# Implementation ledger — design v0.3

The private design document defines this independent repository. Existing
Kubernetes/KubeEdge and Slurm are external compute backends; no other application
API, domain model, source code or database is an implementation dependency.
No private design document or site configuration belongs in this repository.

## Acceptance map

| Milestone | Required evidence | Current evidence |
|---|---|---|
| M0 | Read-only inventory, versions, allowlists, separate backend pools | Read-only Kubernetes/Prometheus inventory and project API verified; Slurm inventory/full hardware discovery pending |
| M1 | KFP launcher → API → suspended Kubernetes Job → result + MLflow | Actual uncached KFP → API → Kueue → RTX 5080 → S3/MLflow verified, including replay |
| M2 | Slurm submission/status/cancel/results on qualified GPU/NPU | GPU F0/accounting historical proof; native guard and result transport implemented; controller unreachable, full model/API path pending |
| M3 | Actual Kueue and Slurm quota/account enforcement, unit-separated ledger | Bounded live quota/priority trials and terminal ledger verified; cross-project isolation and full costs pending |
| M4 | Immutable variants, two signatures, consent/budgets, quality-gated profiles | Consent/budget/pilot loop and real GPU confirmation verified; training isolation pending |
| M5 | Lookup/random/qLogNEI ask/execute/observe/final-confirmation | Real GPU random/qLogNEI exploration and confirmation, baseline retained; rigorous comparisons pending |
| M6 | Authorization, idempotency, failure/recovery, E0–E7, S0–S2 comparison | Initial contract/failure suite and PostgreSQL restore verified; experiments pending |
| M7 | Shared GPU interference and conditional MF/transfer | Explicit extension |

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
is possible while the required NPU hardware remains unavailable.

## Current site constraints (generalized)

The existing control plane and edge nodes use different Kubernetes versions;
they will not be upgraded. Queue API is v1beta2. Slurm is 24.11.5 and currently
has historical one-GPU Jetson F0, SlurmDBD and QOS enforcement evidence.
The controller is currently unreachable, so its current scheduler health and
complete model/API path are not asserted. The DEEPX worker has a PCIe link failure. Runtime job submission
must remain separate from GitOps. Site endpoints and credentials stay outside
this repository. New data has its own database and artifact namespace.
