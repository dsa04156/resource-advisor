# Right-sizing claim acceptance audit

상태: **K8s core 검증 / Slurm 신규 gate BLOCKED / 전체 완료 아님**. 코드 검사, 이전 실장비 증거, 새 실험을
분리한다. 이전 결과는 [baseline audit](right-sizing-gap-audit.md)와
[재계산 기록](evidence/right-sizing-baseline-audit-v2.json)에 보존했다.

| 주장 | 코드 | 테스트 | 실제 실험 | 상태 |
|---|---|---|---|---|
| History 없는 workload는 abstain | service.py, right_sizing.py | test_right_sizing.py | right-sizing-gpu-v1.json / hailo-v1 cold-start | PASS, 신규 3 workload |
| 승인된 pilot/observe로 profile 생성 | study.py, passive.py | study/passive tests | GPU 4 studies / Hailo 원본 observe 3회 | PASS |
| Workload/context signature로 이력 구분 | contracts.py, policy.py | contracts/right_sizing tests | 신규 GPU·Hailo digest/context, strict auditor | PASS; shape range는 자동 qualification 아님 |
| ≥2 실제 accelerator/runtime qualification | policy.py, qualifications.py | qualification tests | GPU F0 4 / Hailo F0 1 | PASS; 등록 장치 전체를 supported로 처리 안 함 |
| 동일 logical workload 후보 비교 | policy.py, study.py | search/study tests | GPU CPU×memory 6개 구성, W1 v2 full reference / W2 incomplete | PASS 자원 비교; GPU/NPU task간 NOT_COMPARABLE |
| 실제 BoTorch ask→native probe→observe | search.py, study.py | test_study.py | 신규 actual BO choices 4개, fallback 0 | PASS; 성능 우위 아님 |
| Random/BO 같은 예산 | study.py | trial mutation tests | 각 5 probes, 같은 wall/device/confirmation budget | PASS |
| 독립 final confirmation | study.py | study/trial auditor tests | W1 3 jobs/study, W2 6 jobs/study | PASS; training profile 재사용 금지 |
| Actual/reference residual+source ID | right_sizing.py, store.py | right_sizing/trial tests | GPU feedback12 / Hailo feedback1 | PASS; descriptive interval만 |
| Actual 결과가 다음 결정에 반영 | service.py, right_sizing.py | lifecycle tests | post-main lookup W1 source6, W2 source9 | PASS K8s; 새 Slurm loop 미완료 |
| Stale/OOD/drift 거부 | uncertainty.py, policy.py | uncertainty/drift tests | 신규 TTL4개 422·Job0 / 기존 load-drift-v1 | PASS; 미래 predictive calibration 보장 안 함 |
| Kubernetes/Kueue native 실행 | backends.py | backend/worker tests | primary GPU101 native Jobs, Hailo5 | PASS |
| Slurm native 실행 보존 | backends.py | Slurm recovery tests | 기존 Orin 실제 실행·cancel·response loss/crash recovery | 기존 PASS; 새 controller 접근 BLOCKED |
| 새 Slurm approved feedback loop | right_sizing.py, backends.py | 공통 terminal tests | 신규 Slurm run 없음 | BLOCKED: controller route / native RPC timeout |
| Profiling/confirmation/fitting 비용 | study.py, accounting.py | strict cost/budget rejection tests | 신규 GPU/Hailo raw+total-cost audit | PASS; historical image/energy unknown |
| 실패/취소/invalid 비용 | accounting.py | terminal/recovery tests | 신규 실패 grid3 / 기존 invalid Slurm·stopped predecessor | PASS; 실패 runtime을 0으로 학습 안 함 |
| Positive/null/negative 보존 | domain auditors | baseline/trial mutation tests | 기존 BO/transfer/MF, 신규 same-selection·비용 회수 실패 | PASS |
| 목표 기여 문장 전체 채택 | 이 표, contribution 문서 | evidence auditors | 새 Slurm loop / 같은 logical GPU↔NPU ranking 미증명 | OPEN; 전체 완료·최적화 성공 금지 |
| Qualified MF-KG | mfkg.py | numerical tests | 실제 calibration NOT_QUALIFIED | 조건부 비활성 |
| Transfer/RGPE | transfer.py, rgpe.py | transfer tests | 기존 157 API jobs, 네 전략 같은 선택 | 기존 null result |
| HAMi/eBPF 개선 | 별도 범위 | 실제 attribution과 분리 | 신규 core trial 없음 | 선택 확장 미완료 |

새 증거의 재계산: [trial auditor](../examples/audit_right_sizing_trial.py),
[GPU raw](evidence/right-sizing-gpu-v1.json), [audit](evidence/right-sizing-gpu-audit-v2.json),
[Hailo raw](evidence/right-sizing-hailo-v1.json), [expiry](evidence/right-sizing-stale-reuse-v1.json).

## 문장별 채택 범위

| 목표 문장의 구절 | 현재 근거 | 채택 범위 |
|---|---|---|
| 사전 자원 특성을 모르는 AI workload | 신규 history 없는 generated GPU workloads, 원본 Hailo observe | 고정된 생성/검증 workload; 임의 코드 일반화 제외 |
| 실행 가능성을 검증 | RuntimeVariant, capability, digest·shape·quality gates | 검증 runtime만, 등록된 모든 장치 supported 아님 |
| 제한된 profiling과 불확실성 기반 탐색 | 실제 constrained qLogNEI + budget + independent confirmation | 방법 구현·실제 탐색, 성능 우위와 calibrated forecast 제외 |
| 적합한 이기종 가속기와 자원 구성 추천 | GPU resource 후보 비교; Hailo 단일 qualified candidate | 동일 task GPU/NPU 성능 ranking은 미증명 |
| Kubernetes와 Slurm 결과를 다음 결정에 반영 | 기존 공통 계약/실행·history; 신규 immutable feedback | 새 K8s closed loop PASS, 새 Slurm loop BLOCKED |
| Profile-guided right-sizing 설계·구현 | 기존 모듈 재사용 + 신규 lifecycle/receipt/auditor | 시스템 계층 기여; native scheduler 기능 개발 주장 금지 |

필수 gate 전체가 닫히기 전에는 전체 완료와 최적화 성공을 선언하지 않는다.


새 총 사용량은 [cost capture](evidence/right-sizing-total-cost-capture-v1.json),
[cost auditor](../examples/audit_right_sizing_cost.py),
[cost result](evidence/right-sizing-total-cost-audit-v1.json)로 재계산한다.
추가 reference의 chrono/context/digest/Job 독립성은
[comparison](evidence/right-sizing-comparison-v1.json)에 분리하며, W2 incomplete를
0 regret으로 채우지 않는다. Startup budget guard와 fitting/acquisition subphase
계측은 고정된 실험 이후 source 변경으로, software 검증과 신규 hardware 검증을
구분한다.
