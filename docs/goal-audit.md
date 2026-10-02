# Full v0.3 completion audit

The objective is the full design, not the subset already implemented. An empty
or indirect evidence cell means incomplete. Synthetic tests cannot satisfy a
hardware acceptance gate. Keep this list open until every required gate has
direct evidence; conditional extensions require an explicit applicability result.

| Gate | Required authoritative evidence | Current state |
|---|---|---|
| Independent project | Own source/API/DB/history and scoped credentials | Implemented |
| Environment inventory | Current node/runtime versions, static/dynamic distinction, backend pools | Site checks exist; automated collector pending |
| GPU/NPU support matrix | Detected/runtime/model/training validation, real model outputs per variant | RTX 5080 CUDA/matmul qualified; other runtimes/models/NPU pending |
| Reproducible workload | Code/model/data/config/precision/work units and two signatures | Contract tests pass |
| Cooperative observation/pilot | Fixed/observe/recommend/pilot paths with approval and immutable context | Real GPU pilot/confirmation completed; training protection pending |
| Training protection | Isolated checkpoint/input/output with no mutation of original training | Pending; training pilots rejected |
| Bottleneck diagnosis | CPU/data-loader/IO/accelerator evidence and uncertainty | Pending |
| KFP launch | Real uncached workflow → API → Kueue admission → GPU job → result | Compilation verified; live chain blocked by existing storage image-pull failure |
| Kueue policy | Real project LocalQueue/ClusterQueue/flavor/priority and over-quota tests | Live GPU quota pending/no-Pod, concurrent queueing and later-high-first CUDA execution verified; cross-project/API chain remains open |
| Slurm execution | Actual sbatch/squeue/sacct/scancel, image/env lock, accelerator reservation | Live GPU F0 and adapter accounting read verified; full API/model/result path pending |
| Slurm policy | Accounting DB/association/QOS enforcement, priority and oversize rejection | Lab enforcement, four rejection cases, account queue limit and real GPU priority order verified; cross-user and API execution gates remain open |
| Results/artifacts | Schema/digest/attempt/work units, artifact ownership and durable storage | 28 result bundles verified in S3/API; large models/checkpoints/retention pending |
| MLflow | Actual parameters/metrics/artifacts/model links, outage recovery | 28 live runs and corresponding result artifacts verified; model registration/failure tracking pending |
| Lookup recommendation | Comparable measured evidence, repeats, quality/memory gates, abstention | Tested |
| Recommendation approval | Immutable configuration/version, expiry, permission recheck | Tested |
| Idempotency/recovery | Response loss, restart, duplicate create/cancel, stale result and epoch | Fault tests pass; real process-crash/backend trials pending |
| R1 BO loop | qLogNEI ask→real GPU execution→observe→next→independent confirmation | Actual qLogNEI GPU loop and independent confirmation completed; wider evaluation pending |
| Search representation | Approved finite conditional space, normalized numeric and unordered categories | Implemented/tested |
| Prediction separation | Model snapshot/run IDs, predictions distinct from measurements | Implemented/tested |
| Budgets | Final reserve, per-device units, preparation/transfer/model/failed-run costs | Durable plan reservation implemented; complete measured cost sources pending |
| R2 fidelity | Paired F1/F2/F3 validation, cost-aware MF-KG within qualified groups | Disabled with reason; implementation/paired data pending |
| R3 transfer | Explicit warm start vs RGPE, independent sources, target validation/fallback | Disabled with reason; implementation/source data pending |
| R4 uncertainty | OOD/stale context, independent confirmation, workload/time holdouts | Confirmation implemented; holdout/stale residual analysis pending |
| R5 interference | Qualified solo/shared pairs, per-workload slowdown, unknown-pair abstention | Conditional follow-up; not claimed implemented |
| Usage ledger | Queue/admission/preparation/run/collection, physical/virtual units, all outcomes | Atomic terminal ledger, evidence-based allocation, unknown-aware summaries/backfill verified with real GPU collector failure; durable cancellation timing and full preparation/utilization costs pending |
| Observability | Missing-vs-stale telemetry, CPU/GPU/NPU/queues/jobs/errors, four integrated views | Initial API metrics; collectors/views pending |
| E0–E7 scenarios | Reproducible live compatibility, execution, quota, contention/failure evidence | Pending |
| B0–B3 / S0–S2 | Equal-budget real measurements, raw data, cost/regret/abstention and null improvements | Single-GPU S0/S1/S2 smoke with null improvement published; rigorous comparison pending |
| S3–S6 | Applicability, paired/source data and qualified strategy comparisons | Conditional; no result claimed |
| Deployment automation | Scoped service manifests/RBAC/secrets, lab provisioning, GitOps static services | Example routes only; full deployment pending |
| Restore | Actual independent DB dump/restore with content comparison | Populated study + all five service tables verified |
| Documentation/demo | Public-safe reproduce commands, raw real measurements, support matrix, limitations | Partial docs and synthetic demo; complete hardware demo pending |
| Final review | Requirement-by-requirement evidence and fixed defects | Ongoing; full completion unproven |

The initial NPU PCIe problem blocks that device's qualification, not independent
implementation work. Other compute resources must be checked for current
readiness before use. Never remove cluster pressure/disconnection protections to
make a demonstration appear successful.
