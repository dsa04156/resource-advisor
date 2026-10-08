# Right-sizing claim acceptance audit

상태: **K8s core 및 새 Slurm feedback 검증 / 전체 범위 완료 아님**. 코드 검사, 이전 실장비 증거, 새 실험을
분리한다. 이전 결과는 [baseline audit](right-sizing-claim-audit.md)와
[재계산 기록](evidence/right-sizing-baseline-audit-v2.json)에 보존했다.

| 주장 | 코드 | 테스트 | 실제 실험 | 상태 |
|---|---|---|---|---|
| History 없는 workload는 abstain | service.py, right_sizing.py | test_right_sizing.py | right-sizing-gpu-v1.json / hailo-v1 cold-start | PASS, 신규 3 workload |
| 승인된 pilot/observe로 profile 생성 | study.py, passive.py | study/passive tests | GPU 4 studies / Hailo 원본 observe 3회 | PASS |
| Workload/context signature로 이력 구분 | contracts.py, policy.py | contracts/right_sizing tests | 신규 GPU·Hailo digest/context, strict auditor | PASS; shape range는 자동 qualification 아님 |
| ≥2 실제 accelerator/runtime qualification | policy.py, qualifications.py | qualification tests | GPU F0 4 / Hailo F0 1 | PASS; 등록 장치 전체를 supported로 처리 안 함 |
| 동일 logical workload 후보 비교 | policy.py, study.py | search/study tests, reference auditor mutation tests | GPU CPU×memory 6개 구성, W1 v2 / W2 v3 full reference | PASS 자원 비교; GPU/NPU task간 NOT_COMPARABLE |
| 실제 BoTorch ask→native probe→observe | search.py, study.py | test_study.py | 신규 actual BO choices 4개, fallback 0 | PASS; 성능 우위 아님 |
| Random/BO 같은 예산 | study.py | trial mutation tests | 각 5 probes, 같은 wall/device/confirmation budget | PASS |
| 독립 final confirmation | study.py | study/trial auditor tests | W1 3 jobs/study, W2 6 jobs/study | PASS; training profile 재사용 금지 |
| Actual/reference residual+source ID | right_sizing.py, store.py | right_sizing/trial tests | GPU feedback12 / Hailo feedback1 | PASS; descriptive interval만 |
| Actual 결과가 다음 결정에 반영 | service.py, right_sizing.py | lifecycle / Slurm auditor tests | post-main lookup W1 source6, W2 source9 / Slurm source4 | PASS 두 backend; Slurm 단일 고정 후보 |
| Stale/OOD/drift 거부 | uncertainty.py, policy.py | uncertainty/drift tests | 신규 TTL4개 422·Job0 / 기존 load-drift-v1 | PASS; 미래 predictive calibration 보장 안 함 |
| Kubernetes/Kueue native 실행 | backends.py | backend/worker tests | primary GPU101 native Jobs, Hailo5 | PASS |
| Slurm native 실행 보존 | backends.py | Slurm recovery tests | 기존 recovery + 신규 Orin Job77–81 | PASS; scheduler 설정 불변 |
| 새 Slurm approved feedback loop | right_sizing.py, backends.py | test_right_sizing_slurm_audit.py | right-sizing-slurm-feedback-v1.json / audit-v1 | PASS: qualification / observe3 / approve / execute / residual / next history |
| Profiling/confirmation/fitting 비용 | study.py, accounting.py | strict cost/budget rejection tests | 신규 GPU/Hailo raw+total-cost audit | PASS; historical image/energy unknown |
| 실패/취소/invalid 비용 | accounting.py | terminal/recovery tests | 신규 실패 grid3 / 기존 invalid Slurm·stopped predecessor | PASS; 실패 runtime을 0으로 학습 안 함 |
| Positive/null/negative 보존 | domain auditors | baseline/trial mutation tests | 기존 BO/transfer/MF, 신규 same-selection·비용 회수 실패 | PASS |
| 목표 기여 문장 전체 채택 | 이 표, contribution 문서 | evidence auditors | 양 backend feedback, GPU 자원 비교 / 같은 AI task GPU↔NPU ranking 미증명 | 넓은 가속기 최적 추천 주장은 OPEN; 성능 최적화 성공 금지 |
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
| Kubernetes와 Slurm 결과를 다음 결정에 반영 | 기존 공통 계약/실행·history; 신규 immutable feedback | 새 K8s loop 및 Slurm observe3→승인 실행→source4 PASS |
| Profile-guided right-sizing 설계·구현 | 기존 모듈 재사용 + 신규 lifecycle/receipt/auditor | 시스템 계층 기여; native scheduler 기능 개발 주장 금지 |

필수 gate 전체가 닫히기 전에는 전체 완료와 최적화 성공을 선언하지 않는다.

2026-10-08 복귀 후의 [새 Slurm loop](reference/right-sizing-slurm-feedback.md)는 아래 과거 BLOCKED
관측과 구분한다. [Raw](evidence/right-sizing-slurm-feedback-v1.json)와
[audit](evidence/right-sizing-slurm-audit-v1.json)은 실제 Job5개/421 GPU 예약초, 독립 승인
결과와 다음 source4를 검증한다. [GPU5후보](all-accelerators.md)는 운영 확장이다.


새 총 사용량은 [cost capture](evidence/right-sizing-total-cost-capture-v1.json),
[cost auditor](../examples/audit_right_sizing_cost.py),
[cost result](evidence/right-sizing-total-cost-audit-v1.json)로 재계산한다.
추가 reference의 chrono/context/digest/Job 독립성은
[comparison](evidence/right-sizing-comparison-v1.json)에 분리하며, W2 incomplete를
0 regret으로 채우지 않는다. Startup budget guard와 fitting/acquisition subphase
계측은 고정된 실험 이후 source 변경으로, software 검증과 신규 hardware 검증을
구분한다.


배포와 검증 범위는 [release evidence](evidence/right-sizing-deployment-verification-v1.json)에
source/CI/보존 hash별로 분리했다. [Slurm read-only check](evidence/right-sizing-slurm-readiness-v2.json)는
새 native execution이 0개임을 명시한다. Source가 배포됐다는 이유로 그 gate를
PASS로 바꾸지 않는다. C4는 finite-N 비용 판별이며 online ROI policy나 외삽한
손익분기 예측이 아니다.

2026-10-08 추가 검증: [W2 v3 raw](evidence/right-sizing-reference-v3.json),
[audit](evidence/right-sizing-reference-audit-v3.json), [계획](evidence/right-sizing-reference-plan-v3.json).
2개 fresh qualification과 30개 grid Jobs가 모두 성공했다. Native startup guard의
own-pilot readback/fit branch도 실제 검증됐으며 insufficient-budget 거절 branch는
software 근거만 있다. W2 v1/v2 실패와 비용을 삭제하지 않았다.
[자동 생성 비교표](right-sizing-comparison-results.md)의 W1/W2 reference 거리는
각각 0.6428% / 0.08547%다. 날짜별 descriptive mean reference이며 optimizer
우위, calibrated forecast 또는 순이익을 뜻하지 않는다.
[확장 총계](evidence/right-sizing-total-cost-audit-v2.json)는 183 native Jobs,
521 GPU 예약초, 14 NPU 예약초와 609 CPU core초를 보고한다.
[Slurm v3 검사](evidence/right-sizing-slurm-readiness-v3.json)는 controller 접속 실패와
Orin RPC timeout이 계속됨을 기록한다. 새 Slurm closed loop와 전체 목표는 OPEN이다.
[Senior infrastructure review](right-sizing-contribution.md)는 얕은 통합, 과장된
기여, 측정·비용·failure 처리의 수정 사항과 남은 한계를 분리한다.
[최신 검증](evidence/right-sizing-validation-v3.json)은 CI source e020445에서
Python3.11/3.13 각각 SQLite 1,150 passed·4 skipped / PostgreSQL 1,154 passed와
새 native hardware 32회 성공을 구분한다. 배포 controller source1b1bb48과
고정 benchmark sourcebb2f834도 별도로 기록했다.

2026-10-08 별도 [전체 가속기 연결 cohort](all-accelerators.md)를 추가했다.
이 기록은 위 기존 연구/비교 결과를 수정하지 않으며 전체 목표를 완료로 승격하지 않는다.

| 추가 주장 | 코드 | 테스트 | 실제 실험 | 상태 |
|---|---|---|---|---|
| GPU5와 NPU5에서 검증 템플릿의 native API 실행 | npu_probe.py / build_npu_target.py / 기존 scheduler adapter | focused108 + accelerator auditor | all-accelerators-20261008-v1.json / native-proof-v2 | PASS 제한된 모델별 lab 실행 |
| 불완전 NPU fingerprint를 추천 근거로 자동 재사용하지 않음 | policy.py | test_npu_probe.py | Console submission warning 및 capability provenance | PASS software guard; firmware qualification 미완료 |
| Rockchip 재실행 transport | backends.py / explicit operator allowlist | test_backends.py | host v2 연속 API2 성공, 전환 뒤 timeout도 보존 | EXPERIMENTAL; 장기 안정성·격리 미검증 |
| 실패 비용을 성공과 함께 보존 | 기존 accounting + audit_accelerator_enablement.py | missing-cost/tamper replay tests | 실패5, GPU 요청 단위459(physical273/slot186)/NPU557/CPU1016초, 실패1 비용null | PARTIAL; 전체 비용/ROI 주장 거절 |
| 새로운 이기종 모델 성능 비교/추천 우위 | 없음 | 없음 | 모델·quality가 달라 NOT_COMPARABLE | OPEN; 주장 금지 |
