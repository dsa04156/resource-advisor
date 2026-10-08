# Slurm 관측 → 추천 → 승인 실행 → 피드백

2026-10-08 복귀 후 [고정 계획](../evidence/right-sizing-slurm-plan-v1.json)의 새 ID
`right-sizing-slurm-20261008-v1`로 실행했다. 기존 native guard, 26,649-path 환경 manifest,
CNN producer, 제한된 SSH gateway와 Slurm adapter를 재사용하고 worker에 동일 runtime의
새 variant binding만 추가했다. Scheduler/gateway 설정·source/image·driver는 그대로다.

| 단계 | Native Job | 결과 | GPU 예약초 |
|---|---|---|---:|
| 원본 qualification 관측 | 77 | SUCCEEDED | 89 |
| 새 workload 원본 관측 1 | 78 | SUCCEEDED | 85 |
| 원본 관측 2 | 79 | SUCCEEDED | 83 |
| 원본 관측 3 | 80 | SUCCEEDED | 84 |
| 추천 승인 후 독립 실행 | 81 | SUCCEEDED / feedback COMPARABLE | 80 |
| 전체 | 5개 | 모두 성공 | 421 |

GPU1/CPU1/메모리1GiB와 native 120초 한도를 유지했다. 짧은 pilot을 추가하지 않았다.
400개 numerical comparison, native sm_87, runtime version과 cgroup 경계를 검증했다.
새 signature는 최소 독립 실행 3회 quality 계약을 포함해 과거 1회 계약 profile과
구분한다. History 없는 시점에 `NEEDS_PROFILE`로 abstain했다. 실제 결과3개로 lookup
추천과 명시적 API 승인을 거쳐 독립 실행했다. `VERIFIED` lifecycle/immutable feedback을
저장했으며 다음 recommendation의 source4개에 그 결과가 포함됐다.

Reference forward 평균은 **12.069607ms**, descriptive 평균 구간은
**10.187056–13.952157ms**다. 승인 실행은 **11.986714ms**, residual은 **−0.686787%**다.
같은 단일 구성이므로 자원 선택의 성능 개선이나 미래 predictive calibration이 아니다.
전체 비용은 **421 GPU 예약초 / 421 CPU core 예약초 / 431,104 memory MiB초**이며 protocol
wall time은 **549.050608초**다. Full verification/startup도 reservation에 포함한다.
Actual model CPU time, 재사용 환경 준비, 에너지와 서비스 CPU는 unknown이다.

[Raw JSON](../evidence/right-sizing-slurm-feedback-v1.json),
[CSV](../evidence/right-sizing-slurm-attempts-v1.csv),
[audit](../evidence/right-sizing-slurm-audit-v1.json),
[auditor](../../examples/audit_right_sizing_slurm.py)가 수치를 재현한다. Native parent/step,
signature/model/input digest, 독립 source/target, cost/budget/quality/feedback를 확인한다.
각 attempt는 usage1개/MLflow run1개, S3/API/MLflow bytes가 일치한다. 회귀 검사14개는
정상 재계산과 duplicate/leakage/digest/quality/cost/reservation/budget/confirmation/
cgroup/publication 오류 거절을 확인한다.

공통 계약을 두 실제 backend에서 연결한 증거다. **단일 고정 Slurm 후보 lifecycle**이며
Slurm active search, 동일 AI task GPU↔NPU 순위, backend 속도 비교나 DDP가 아니다.
기존 Random/qLogNEI 동일 선택과 N≤3 profiling 비용 회수 실패는 변하지 않는다.

추천 TTL 15분이 지난 후 [live readback](../evidence/pool-resume-readback-v1.json)은
`NEEDS_RECONFIRMATION`을 반환했다. 실행 당시 `VERIFIED`와 이후 만료를 구분한다.
과거 승인과 추천을 자동 부활시키지 않았으며 유효한 profile로 새 추천을 요청하는
절차가 필요하다. 관측 실행 catalog와 backend의 현재 가용성은 별도 상태다.
