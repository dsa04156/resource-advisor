# 실제 right-sizing 비교 결과

이 표는 raw evidence와 auditor에서 자동 생성한다. 전체 완료 또는 최적화 성공을 선언하지 않는다.
새 Slurm approved-feedback 실험은 controller 연결 복구가 필요하다.

## Static / Random / qLogNEI

각 arm의 main 실행은 독립 native Job 3개다. 두 탐색은 각각 같은 5-probe 예산을 사용했다.
시간은 W1의 100회 matmul 합계, W2의 12개 CNN block 합계다.

| 작업 | 방법 | 선택 CPU / memory | main 평균 (s) | Static 대비 평균 차이 |
|---|---|---|---:|---:|
| W1 | random | cpu1-mem2048 | 0.046748409 | +0.0182% |
| W1 | static | cpu1-mem2048 | 0.046739913 | +0.0000% |
| W1 | qlognei | cpu1-mem2048 | 0.046611692 | -0.2743% |
| W2 | qlognei | cpu2-mem2048 | 0.260731440 | -1.1500% |
| W2 | static | cpu1-mem2048 | 0.263764764 | +0.0000% |
| W2 | random | cpu2-mem2048 | 0.260848683 | -1.1056% |

Random과 BO가 두 작업에서 같은 구성을 골랐다. 평균 차이를 BO의 인과적 성능 우위나 통계적 유의성으로 해석하지 않는다.

| 작업 | 방법 | probes / 서로 다른 후보 | 독립 확인 Jobs | study elapsed (s) | model/planning (s) | 실제 BO 선택 |
|---|---|---:|---:|---:|---:|---:|
| W1 | random | 5 / 4 | 3 | 214.319 | 0.000137 | 0 |
| W1 | qlognei | 5 / 5 | 3 | 220.488 | 2.218690 | 2 |
| W2 | random | 5 / 4 | 6 | 297.215 | 0.000205 | 0 |
| W2 | qlognei | 5 / 5 | 6 | 296.525 | 0.148725 | 2 |

## 나중에 측정한 전체 후보 reference

Grid는 전체 6개 후보의 3회 독립 확인이다. Equal-budget 탐색 경쟁자가 아니며 search/main 이후에 측정했다.
W1은 v2, W2는 다음 날의 v3이다. 날짜별 환경 변동을 통제한 동시 oracle이 아니다.

| 작업 | reference 완료 | 후보 | 독립 확인 평균 (s) |
|---|---|---|---:|
| W1 | 예 | cpu0-5-mem1024 | 0.095877831 |
| W1 | 예 | cpu0-5-mem2048 | 0.093952370 |
| W1 | 예 | cpu1-mem1024 | 0.047264426 |
| W1 | 예 | cpu1-mem2048 | 0.046961792 |
| W1 | 예 | cpu2-mem1024 | 0.046678073 |
| W1 | 예 | cpu2-mem2048 | 0.046661846 |
| W2 | 예 | cpu0-5-mem1024 | 0.417030732 |
| W2 | 예 | cpu0-5-mem2048 | 0.400347004 |
| W2 | 예 | cpu1-mem1024 | 0.263946543 |
| W2 | 예 | cpu1-mem2048 | 0.263579347 |
| W2 | 예 | cpu2-mem1024 | 0.260518063 |
| W2 | 예 | cpu2-mem2048 | 0.260740732 |

| 작업 | 방법 | 선택 | reference 최소 평균과의 거리 |
|---|---|---|---:|
| W1 | random | cpu1-mem2048 | 0.6428% |
| W1 | qlognei | cpu1-mem2048 | 0.6428% |
| W2 | random | cpu2-mem2048 | 0.0855% |
| W2 | qlognei | cpu2-mem2048 | 0.0855% |

## 초기 비용을 포함한 실제 N=3

Qualification + profiling/confirmation + 실제 main 실행을 합한다. Model/planning은 study elapsed에 이미 들어 있어 중복 가산하지 않는다.
Latency cost는 개별 Job/API latency 합이며 전체 연구 wall-clock과 다르다.

| 작업 | 방법 | 누적 latency cost (s) | GPU 예약초 | CPU core초 |
|---|---|---:|---:|---:|
| W1 | static | 56.463414 | 13 | 15 |
| W1 | random | 274.443631 | 34 | 37 |
| W1 | qlognei | 274.687254 | 36 | 40.5 |
| W2 | static | 77.892482 | 13 | 16 |
| W2 | random | 376.872685 | 45 | 64.5 |
| W2 | qlognei | 375.268462 | 43 | 63 |

**Random과 BO 모두 실제 N=1..3에서 profiling 비용을 회수하지 못했다.** 측정하지 않은 반복 횟수까지 손익분기를 외삽하지 않는다.

## 추가 reference 비용과 보존한 실패

W2 v3은 새 qualification을 포함해 32 native Jobs, 100 GPU 예약초, 108.5 CPU core초, 실제 protocol 942.774초다.
전체 연구에는 실패한 v1/v2 reference와 v3의 모든 예약 비용을 함께 포함한다.

- GPU: 178 Jobs / 521 예약초
- NPU: 5 Jobs / 14 예약초
- CPU: 609 core초
- GPU 연구 Jobs의 host memory: 878592 MiB초
- 실패한 GPU Jobs: 3개, 비용 포함
- GPU protocol wall 합계: 5083.544초; calendar envelope 55470.616초는 실험 사이 중단 시간도 포함

GPU/NPU 예약초는 같은 경제적 비용이 아니며 GPU busy time도 아니다. Image/fixture 준비, 에너지, shared service CPU 비용은 이번 합계에서 unknown이다.
Hailo qualification/승인 feedback은 별도 qualified ResNet50 계약이다. W1/W2와 NOT_COMPARABLE이며 GPU↔NPU 속도 순위를 주장하지 않는다.

## 원시 근거와 그림

- [Primary raw](evidence/right-sizing-gpu-v1.json), [원래 계획](evidence/right-sizing-trial-plan-v1.json)
- [W1 v2 / 실패한 W2 v2](evidence/right-sizing-reference-recovery-v2.json)
- [W2 v3 raw](evidence/right-sizing-reference-v3.json), [v3 계획](evidence/right-sizing-reference-plan-v3.json), [v3 audit](evidence/right-sizing-reference-audit-v3.json)
- [전체 비용 입력](evidence/right-sizing-total-cost-capture-v2.json), [전체 비용 audit](evidence/right-sizing-total-cost-audit-v2.json)
- [누적 비용 그림](evidence/right-sizing-figures-v3/right-sizing-cost-v3.png), [전체 후보 그림](evidence/right-sizing-figures-v3/right-sizing-reference-v3.png)
- [Claim audit](right-sizing-claim-audit.md): 새 Slurm loop, 같은-task GPU/NPU ranking, BO 우위, calibrated forecast, net benefit은 미증명
