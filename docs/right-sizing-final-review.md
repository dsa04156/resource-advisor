# AI infrastructure 관점 최종 검토

검토 범위: 기존 저장소를 유지한 right-sizing core와 새 비교 증거.
전체 목표 완료와 production-ready 판정은 하지 않는다. 새 Slurm 승인 feedback
loop가 실장비에서 닫히지 않았다.

| 검토 항목 | 발견 / 수정 / 증거 | 남은 범위 |
|---|---|---|
| Scheduler 기능을 자체 기여로 포장 | Native admission/binding/backfill과 resource-advice 계층을 [기여 문서](right-sizing-contribution.md)에서 분리 | Native scheduler 개선 주장 금지 |
| 얕은 experiment integration | 새 grid 30개 API 결과를 S3/API/MLflow에서 byte readback, 동일 attempt별 단일 run 확인 | MLflow 자체 구현, registry/HA 전체 완성 주장 금지 |
| Raw 숫자와 요약 불일치 | Study observation이 signed result와 다르면 auditor가 거절하도록 수정; 조작 회귀 테스트 추가 | 원격 시스템의 attestation을 독립 재실행한 것과 구분 |
| Confirmation에 startup 예산 부족 | 실패 3개 보존; own-pilot cap guard 추가; 사전 고정 W2 v3 예산에서 32 Jobs 성공 | Insufficient-cap 거절은 software 검증, 미래 startup 보장 없음 |
| 일부 후보만 측정하고 최적이라고 표현 | W1 v2 / W2 v3 각각 6개 후보·3개 독립 확인; incomplete v1/v2도 보존 | 날짜가 다른 descriptive reference, true oracle 아님 |
| 초기 비용 숨김 | Qualification/profiling/confirmation/main과 실패 reservation 포함; GPU/NPU/CPU 단위 분리 | Energy/image construction/shared-service CPU unknown |
| BO를 기본 승자로 표현 | 같은 5-probe 예산에서 Random/BO 동일 선택, 실제 N=1..3 비용 회수 실패 기록 | Superiority, 통계적 유의성, 외삽한 손익분기 주장 금지 |
| 장치 탐지를 workload 지원으로 표현 | GPU model/runtime 및 Hailo HEF/quality qualification과 inventory 분리 | DEEPX/AMD/다른 NPU 지원이나 같은-task GPU/NPU ranking 미증명 |
| 배포됐으므로 Slurm 완료라고 표현 | API/GPU/Slurm worker source/image 일치 확인과 실제 native run을 분리; controller TCP 실패·sinfo timeout 공개 | Controller 복구 뒤 fresh qualification→observe→approval→execution→feedback 필요 |
| 선택 기법·기술을 불필요하게 활성화 | MF-KG는 physical fidelity qualification 실패 시 비활성; 기존 transfer null 보존 | Sharing/eBPF/DDP/large-cluster 성과를 core에 포함하지 않음 |
| 실험이 기존 작업에 미치는 영향 | 기존 immutable entity 3,981개 digest, terminal Job 806개 state/version 보존; 기존 usage 806개 ID 보존 | Old ledger body 전체는 새 실험 preflight에서 따로 hash하지 않았음 |
| 실험 후 자원 정리 | Own Job UID/Pod UID를 확인해 terminal Pod 2개 삭제, 관련 Pod 잔여0; Job/result/artifact 유지 | 다른 작업이나 cluster/driver 설정 변경 없음 |

근거: [새 reference auditor](../examples/audit_right_sizing_reference.py),
[trial auditor](../examples/audit_right_sizing_trial.py),
[비용 auditor](../examples/audit_right_sizing_cost.py),
[생성된 비교표](right-sizing-comparison-results.md),
[보존·정리 기록](evidence/right-sizing-reference-cleanup-v3.json),
[배포 readback](evidence/right-sizing-deployment-readback-v2.json),
[Slurm blocker](evidence/right-sizing-slurm-readiness-v3.json).

면접에서 설명할 수 있는 범위는 검증된 compatibility, 제한된 profiling,
독립 확인과 승인, native 실행, immutable feedback, 비용 평가와 null result다.
모든 이기종 장치의 최적 배치나 새 Slurm closed loop를 완료했다는 설명은
[claim audit](right-sizing-claim-audit.md)의 OPEN gate가 닫힌 뒤에만 가능하다.
