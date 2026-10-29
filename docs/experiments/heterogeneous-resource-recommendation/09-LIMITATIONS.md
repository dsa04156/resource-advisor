# 09. 한계와 미지원 범위

## 증거 수준

| 수준 | 현재 |
|---|---|
| 구현 + software test | opt-in 추천(B3–B5), pool target, evidence 계약·import, linkage, collector, 고정 runner·분석 |
| 오픈소스 native 기능 | Kueue admission·quota, kube-scheduler bind, Slurm 할당·feature constraint (재사용, 수정 없음) |
| 실장비 검증 | **없음.** 이 단계는 Job 제출·운영 변경을 하지 않았다 |
| 결과·그림 | **없음.** 새 측정이 없으므로 08/10 결과 문서와 차트는 만들지 않았다 |

## 미지원·미구현

1. **작업 family 계약 없음.** 비교는 한 `WorkloadSpec` 안의 후보(같은 precision·model·input)만이다. FP32 CUDA와 INT8 HEF처럼 compiled precision이 다른 GPU↔NPU variant는 같은 순위에 들어가지 않는다. E5의 GPU↔NPU 비교는 이 계약 없이는 불가.
2. **Queue 완전성은 collector 품질에 의존.** Kueue 밖 장치 점유는 occupancy를 모르므로 snapshot이 incomplete가 되고 B4/B5는 abstain한다. 오늘처럼 다른 project Deployment가 장치를 쓰면 B4/B5는 대부분 `ESTIMATE_UNAVAILABLE`일 것이다. Slurm queue snapshot collector는 아직 없다(operator import만).
3. **Profile 유도 경로의 phase 분해 한계.** RA Job에서 유도한 profile은 admission·runtime startup·compute·unattributed만 가진다. data staging·model preparation·communication·finalization은 workload가 별도 경계를 보고해야 생긴다. 그래서 RA 유도 profile로는 B5 대체가 `STAGING_NOT_SEPARABLE`이다.
4. **Pool mode.** Kubernetes는 hostname In affinity로 안전하게 제한하지만, pool selector label의 writer 통제(NodeRestriction 등)는 운영 설정에 달려 있다. Slurm pool은 operator가 배타 feature 근거를 등록해야 하며 오늘 그런 feature는 없다. Kueue flavor가 hostname key를 무시할 수 있어 admission 성공이 member 가용성을 보장하지 않는다. pool 모드 실장비 실행은 없다.
5. **불확실성.** profile의 반복 간 표준편차를 서술용으로 보존할 뿐, 예측 구간·신뢰도는 없다. 과거 JCT 실험에서 queue-aware 선택이 평균 JCT를 개선하지 못한 기록이 있다(감사 §3). 같은 결과가 다시 나올 수 있다.
6. **E7의 실장비 부분, E8.** E7은 software fault injection만 있고, E8은 원천 실험이 없어 BLOCKED.
7. **범위 밖.** 전역 GPU 예약, 스케줄러 간 물리 장치 공동 소유, 실행 중 migration, KAI·HAMi·DRA·TAS 통합, 최적성 주장.
8. **E1–E4.** 오늘 장비로 BLOCKED/NOT_VALIDATED([07 §3](07-EXPERIMENT-PLAN.md#3-고정된-준비-계획-2026-10-29)). 결과를 만들어 넣지 않는다.

## 기존 테스트 실패

전체 테스트 결과와 기존 실패(이번 변경과 무관한 것)는 dispatch 보고에 따로 기록한다.
