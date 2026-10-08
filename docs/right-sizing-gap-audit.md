# Right-sizing baseline gap audit

Audit date: 2026-10-07. Baseline: `5d8b395` on the existing
`dsa04156/resource-advisor` repository. This is an audit, **not a completion
declaration or a new hardware experiment**. The four uncommitted scheduler-lab
priority files predate this goal; their changes are excluded from baseline claims.

The requested README and nineteen baseline reports were reviewed alongside the
tracked source/test/example/deployment tree, Pydantic API contracts, SQLAlchemy
tables, optimizer/coordinator, result ingestion, reuse checks and backend paths.
There are 575 tracked files: 61 source files, 84 test modules, 85 example files,
26 deployment files, 19 automation files and 294 documentation/evidence files.
The tree includes 57 source Python modules and 537 source functions/methods;
these inventory counts are not test passes or hardware qualification. A
machine-readable baseline inventory records file hashes separately.

“실장비 증거” below means preserved, attributable historical hardware evidence.
It does not assert that those nodes are accessible or freshly qualified today.
Existing reports' narrower acceptance boundaries and failed attempts remain valid.

| 목표 기능 | 현재 구현 | 실제 하드웨어 증거 | 부족한 부분 | 구현 필요 여부 |
|---|---|---|---|---|
| RQ1 처음 보는 workload의 abstention | `service.py:recommend`, `policy.py:compatibility`; history 없이 `NEEDS_PROFILE`, 후보 없이 `NO_COMPATIBLE_VARIANT` | `evidence/contract-boundaries-v1.json`: fresh CUDA qualification 후 ranking 비어 있음, approval/pilot 거절, 새 API Job 0 | 전체 lifecycle을 한 read API로 연결한 계약 없음 | 기존 guard 재사용; lifecycle projection 추가 |
| UNKNOWN→VERIFIED lifecycle | Workload/Study/Recommendation/Approval/Job/Profile 각각 DB 계약 존재 | GPU 승인 실행, Hailo 승인 확인, drift follow-up은 각각 저장됨 | 통합 상태, 상태별 근거 ID, 재확인 우선순위와 verification 의미를 명시해야 함 | 필요; 기존 Job state 변경 없이 additive API |
| observe / cooperative pilot 분리 | `passive.py`, `passive_collector.py`; `study.py:create` consent/entrypoint/reserved plan 검사 | `passive-gpu-observation-result-v1.json`: unchanged CUDA program, 12 samples; policy v2의 실제 consented probes | Slurm/NPU passive sensor 미검증; arbitrary code의 quality/step/throughput unknown | 핵심 경로 재사용; 미검증 sensor supported 처리 금지 |
| WorkloadSignature | `contracts.py:WorkloadIdentity`; code/model/data/config/shape/task/precision/batch/global batch/work units/quality digest | contract boundary의 shape/batch/precision별 별도 scope | optimizer/model logical name과 input range는 별도 명시 field 없이 digest/정확 shape로 표현 | legacy digest 보존하는 optional descriptor 검토 |
| ExecutionContextSignature | `policy.py:context_signature`; backend/arch/image/environment/runtime/device/mode/count/CPU/memory/parameters/command | GPU/Hailo/Slurm result envelopes의 두 서명; runtime/arch negative checks | host CPU identity, runtime flags의 구조화된 명시성; 기존 unknown를 값으로 추정 금지 | optional descriptor; 기존 evidence hash 불변 |
| RuntimeVariant / capability gate | DETECTED/RUNTIME_VERIFIED/MODEL_VERIFIED/TRAINING_VERIFIED, shape/model/runtime/reservation 검증 | RTX5080 CNN, Slurm Orin CNN, Hailo-8 ResNet50 실제 결과 | 다른 모델/SDK에는 qualification 재필요 | 재사용 |
| 2종 이상의 실제 accelerator/runtime | CUDA x86/Jetson, HailoRT/compiled HEF 각 검증 | `slurm-api-v1.json`, `hailo-resnet50-platform.json`, CUDA policy evidence | 서로 다른 모델 결과를 같은 workload 비교로 합산할 수 없음 | 등록 재구현 불필요; comparable group 검증 필요 |
| 동일 logical task의 이기종 비교 | workload-first CUDA smoke pooling; variant별 immutable context | 여러 NVIDIA CUDA smoke 성공; AI CUDA CNN과 Hailo ResNet50는 서로 다른 logical workload | 동일 AI workload의 ≥2 runtime 실제 paired 비교 없음. HEF precision/quality/memory boundary 차이 존재 | 필수 비교 실험 후보 조사; 불가능하면 NOT_COMPARABLE 명시 |
| DEEPX/AMD/다른 NPU | 장치 inventory와 실행 qualification 분리 | Pi PCIe readback은 accelerator 미탐지; Mobilint telemetry는 model evidence 아님 | DEEPX runtime/model, AMD 실장비 증거 없음 | BLOCKED / interface-only; supported 또는 실측 주장 금지 |
| Right-sizing mutable scope | `WorkloadSpec.approved_space`: CPU/memory/parameters 승인; accelerator count 변경 거절 | policy/transfer의 CPU .5/1/2 + approved threads; GPU1 고정 | backend/variant 변경의 승인 의미 및 동등 semantics를 명시해야 함 | 기존 contract 강화; GPU-count 탐색 추가 안 함 |
| 병목별 evidence | serial phase, kernel profiler, cgroup PSI/I/O, NVML brackets | `e5-kernel-v2.json`, `load-drift-v1.json` | 실제 network/storage attribution 및 eBPF 비교 없음 | 기존 phase 재사용; eBPF 조건부 |
| S0 lookup / S1 random / S2 qLogNEI | actual BoTorch ModelListGP/qLogNEI; one-hot category, positive negative-log time, memory/quality constraints, seeded fallback | `policy-comparison-v2.json`: 15 BO choices, fallback0, 각 cold study8 probes | 보다 넓은 workload/device effectiveness; model fitting 세부 시간은 planning 시간에 포함 | 알고리즘 재구현 불필요 |
| Equal-budget search | durable device/wall/probe/final-reserve budgets | 세 temporal blocks의 lookup/random/BO cap 동일; lookup source cost 차감 | Grid 정책의 독립 equal-budget 비교 없음; reference grid는 뒤에 실행한 characterization | 새 비교 설계 시 명시; 현재 결과 우위 주장 금지 |
| Independent confirmation | study finalist/baseline fresh confirmation, pilot은 general history 제외 | policy v2 129 application Jobs, transfer157; source/target/final ID 분리 | 새로운 이기종 연구 실험에도 동일 gate 필요 | 재사용 |
| Adaptive replication | min/max independent runs, descriptive RSE stopping, reserve | 22 actual CUDA attempts, 44 GPU-s; precision-met / cap-unresolved 분리 | random/BO와 equal-total-budget superiority 비교 없음 | 재사용; 우위 주장 금지 |
| Multi-fidelity | immutable fidelity spaces, paired qualification, actual MF-GP/MF-KG conditional executor | calibration/sampling/thermal actual trials; qualified group 없음 | live qualified MF-KG가 없음; short/tie/bias evidence는 fail | 조건부 BLOCKED; 기준 완화나 post-hoc qualification 금지 |
| Transfer/RGPE | operator family/provenance, balanced target checks, rank weights, target-only fallback | `transfer-gpu.json`:157 Jobs, 네 전략 모두 CPU2 | live harmful transfer 및 일반화 미검증 | 재사용; selection advantage 없음 유지 |
| Uncertainty / OOD | scope/shape/age/context/repeat gates, descriptive intervals; offline chronological forecast audit | historical coverage0/2; retrospective numerical holdout in-range9, OOD18 abstentions | operational calibrated predictive coverage 없음; live numerical zero-shot 추천 아님 | abstention 재사용; uncertainty 의미 명시 |
| Explicit approval / revalidation | immutable digest, expiry, project check, approval/submit/remote boundary 재검사 | approved GPU/Hailo; drift old approval422, no new Job | unified recommendation response에 age/context/reprofiling reason 묶음 필요 | additive read contract |
| Approved execution feedback | `uncertainty.py:assess_recommendation` reads matching post-recommendation attempts and residuals; ingestion updates history | GPU approved execution, Hailo out-of-mean interval verification, drift3 consecutive residuals | residual은 read-time 계산; approved execution에 immutable forecast/actual receipt를 직접 결합한 entity 없음 | 필요; existing evidence rewrite 없는 receipt |
| Drift latching | ±25%, 3 consecutive independent follow-ups; recovery does not revive old recommendation | ten actual CUDA Jobs, 197 GPU-s; workload/recovery/negative approval saved | heuristic이며 causal/calibrated detector 아님 | 재사용 |
| Kubernetes/Kueue execution | suspended Job → LocalQueue admission → native scheduler → collector | CUDA policy/KFP, CPU→GPU, Hailo real Kueue execution | 전체 native failure matrix는 별도 미완료 | 핵심 path 변경 금지 |
| Slurm native execution | validated sbatch/squeue/sacct/scancel, account/QOS/partition, result transport | Orin actual API result and accepted-response loss/SIGKILL recovery | 같은 workload의 profiling→recommend→approved→feedback loop Slurm에서 미검증; 마지막 isolation job unresolved | 새 bounded loop; 현재 접근 불가 시 BLOCKED |
| Idempotency / recovery / isolation | project-scoped unique keys, epoch/outbox/lease/owner route guards, cancel confirmation | K8s/Slurm response recovery, two-project native acceptance; retained failures | full HA/distributed exactly-once/Slurm failure matrix 미검증 | 보존; right-sizing로 약화 금지 |
| Usage / failed costs | one terminal ledger transaction, observed allocation or unknown, CPU/device units 분리 | failure/cancel/result-invalid, Slurm sacct, terminal retention | unresolved allocations, lost-process planning/external prep/energy costs 미측정 | unknown를 포함한 completeness/claim guard 필요 |
| MLflow/S3/result linkage | result ownership/digest, independent outbox delivery/readback | policy129 / transfer157 / operational26 API Jobs exact bytes, server-outage recovery | model registry와 full tenant auth는 별도 미완료 | 기존 linkage 재사용 |
| RQ3 first-use / actual N costs | operational evaluator/plot; qualification/profiling/confirmation/main separated | compute0.998% 감소지만 6회 내 비용 회수 실패; B2 setup354.439s/49GPU-s | 범용 right-sizing 비용 auditor, unknown 비용으로 경제성 확정 금지 | auditor wrapper와 새 experiment별 비용 보고 |
| 원시 provenance / auditor | 여러 domain별 auditors, immutable plans, digests, native/run IDs, CSV/plots | hardware evidence JSON/CSV 및 prior failures 보존 | 핵심 claim을 하나의 acceptance manifest에서 machine-check할 도구 없음 | 추가; existing domain auditors 재사용 |
| W1/W2/W3 coverage | matmul, generated CNN, Hailo trained ResNet50 각각 실행 가능 | matmul null result, host/input-sensitive CNN studies, Hailo accuracy80%/agreement97% | 한 frozen design으로 3class right-sizing 비교는 없음; hetero comparability 선결 | 새로운 ID/plan으로 가능한 실험만; 기존 결과 대체 금지 |
| Public safety / deployment | site config repo 밖, scoped manifests, pinned Argo static services, lab Ansible | GitOps/Ansible/recovery readbacks | production HA/large-cluster/energy/eBPF/HAMi study 미완료 | core claim과 분리; private evidence 익명화 |

## Research questions and immediate implementation order

RQ1–RQ5 are the questions specified in the current goal: bounded discovery,
evidence-based resource selection, net benefit including profiling, changed
environment revalidation, and a common contract across native backends. Passing
a functional gate does not imply an advantage over random/grid/static requests.

1. Add a read-only workload right-sizing lifecycle/evidence projection. Reuse
   compatibility, signatures, studies, recommendations, approvals and native Jobs;
   do not introduce a competing scheduler or rewrite existing state.
2. Bind approved executions to immutable recommendation/actual feedback receipts,
   with source IDs and constraint status. Preserve existing residual drift policy.
3. Re-run existing domain auditors and compose a provenance/cost/claim auditor.
4. Inspect current reachable runtimes and preregister new missing hardware loops.
   New qualification and experiment costs remain separate, including failures.
5. Write the contribution and claim audit using only proven gates. Record blocked
   hardware steps with the exact resume prerequisite; proceed with independent work.

## Baseline results that must remain unchanged

- Policy v2: no BO selection advantage; 361 GPU reservation seconds, plus the
  stopped predecessor's140, equals501 across those two protocols.
- Operational trial:35 actual GPU Jobs; approximately1% shorter compute, but
  profiling354.439 seconds/49GPU-s not recovered within six measured uses.
- Transfer:157 application Jobs,475GPU-s including recorded qualifications;
  random/BO/warm-start/RGPE all selected CPU2. No transfer selection advantage.
- Hailo: ResNet50 passed80% accuracy/97% agreement; the later approval result
  missed the mean interval. Earlier ResNet18/EfficientFormer failures remain.
- Fidelity: existing physical groups remain unqualified. Synthetic MF-KG tests
  are not physical qualified trials. DEEPX remains blocked by missing PCIe device.
- Scheduler-lab multi-GPU barriers and motion are operational demos, not DDP,
  scaling-efficiency, AI-model portability or profile-guided speedup evidence.

Overall acceptance remains **OPEN**. This audit identifies implementation gaps;
it does not retrospectively mark current records as a new experiment.

## Additive validation after baseline

The immutable baseline above is preserved. The [current claim audit](right-sizing-claim-audit.md)
and [generated comparison results](right-sizing-comparison-results.md) now link the new
lifecycle/feedback implementation, three qualified workload paths, primary equal-budget
Random/BO results and complete later finite references (W1 v2, W2 v3). The original
failed references remain separately recorded and charged. Fresh W2 v3 adds 32 native
Jobs with no failures; it does not replace the primary comparison or imply BO advantage.
New Slurm approved-feedback hardware validation remains blocked by controller connectivity.
