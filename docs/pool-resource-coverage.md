# 현재 자원 풀과 실행 후보

2026-10-08 실제 등록·큐·실행을 확인했다. 장비 목록은 전체 자원이며 작업 후보는
그 코드·runtime·품질 조건을 검증한 부분집합이다.

| 자원 | 현재 노출 | 실행 범위 |
|---|---|---|
| RTX5060Ti / RTX5080 / GB10 | Kubernetes GPU 각각 1 | 공통 CUDA 작업; RTX5080은 별도 PyTorch 연구 경로도 있음 |
| Orin Nano / AGX Orin | 각각 shared 슬롯 2 | 공통 CUDA 작업; 슬롯 수는 물리 GPU 수가 아님 |
| Hailo-8 두 장치 | 각각 NPU 1 | 같은 compiled ResNet50/100 input의 자동 후보 실행 |
| Mobilint ARIES2 | NPU 1 | compiled Candy 반복 출력 검증; 스타일 품질 평가는 아님 |
| Intel NPU3720 | NPU 1 | 직접 NPU 생성 CNN과 NumPy 수치 기준; RTX5080과 같은 호스트 |
| Rockchip RK3399Pro | NPU 1 | 공식 ResNet18 단일 입력; host transport lab 전용, 연속 API 성공2 |
| Slurm Orin | GPU GRES 1, node IDLE | 고정 CNN 관측·추천·승인·피드백 |
| Slurm Pi | CPU node IDLE, NPU GRES 없음 | NPU 실행 지원을 의미하지 않음 |

Kubernetes GPU 노드는 5대다. 일반 GPU resource 3개와 공유 슬롯 4개를 더한 7을
물리 GPU 수라고 부르지 않는다. Backend 등록 수를 독립성 검증 없이 합산하지 않는다.

기존 `cuda-sustained-auto-v1`은 네 GPU만 후보로 등록했다. RTX5080은 별도 workload여서
선택되지 않았다. [고정 계획](evidence/pool-extension-plan-v1.json)에 따라 RTX5080의
동일 90초 integer kernel을 실제 검증하고 `cuda-sustained-auto-v2`에 추가했다.
기존 네 qualification timestamp와 결과는 갱신하지 않았다.

Console 기본 작업 목록은 v2로 연결했고 **GPU 5대 · 자동 자원 선택 · 90초** 템플릿도
등록했다. 사용자는 GPU를 지정하지 않는다. 호환성·요청 headroom·최근 할당을 평가해
native queue로 제출한다. 성능 최적화와 원자적 예약을 보장하지는 않는다.
[실제 요청/결과](evidence/pool-extension-evidence-v1.json)는 다섯 후보 평가 후
RTX5060Ti 경로의 한 API 작업 성공을 기록한다. RTX5080 qualification 91 GPU 예약초와
자동 실행 91 GPU 예약초, 총 182 GPU 예약초다. 이번에 다섯 장비를 동시에 실행했다고
주장하지 않는다. CUDA smoke는 임의 AI 모델의 실행·성능 qualification이 아니다.

NPU는 CUDA 프로그램의 직접 실행 후보가 아니다. Hailo는 별도 검증 HEF·입력·quality
계약의 ResNet50 작업으로 사용한다. Mobilint/Intel/Rockchip은 각 검증 템플릿의 수동
observe만 연결했다. firmware identity가 불완전해 성능 추천 재사용은 보류한다.
최근 W1/W2 비교도 동일 workload/runtime 검증 범위인 RTX5080에 한정했으며
다른 모델의 NPU 결과와 속도 순위를 합치지 않는다.

Slurm 컨트롤러 재가동 후 남은 `Not responding` DOWN 상태는 빈 큐와 daemon 확인 후
두 lab node에 `RESUME`을 적용해 복귀시켰다. `ReturnToService=0`, driver와 KubeEdge는
변경하지 않았다. 새 [Slurm loop](right-sizing-slurm-feedback.md)까지 두 실험의 총계는
7 native Jobs / 603 GPU 예약초 / 603 CPU core 예약초이며 **그 이전 cohort**에는 새 NPU 실행이 없다.
[보존·정리](evidence/pool-resume-preservation-v1.json)는 기존 entity body digest31,962개,
terminal Job state/version836개, usage body digest836개를 확인한다. 소유 terminal Pod1개를
삭제했고 잔여0이다. Job/accounting/result/artifact와 기존 실패 기록은 유지했다.

그 후 별도 [전체 가속기 cohort](all-accelerators.md)를 추가했다. GPU 5대와 NPU 5개에서
API 실행을 확인했고 원시 결과는 이전 파일을 덮어쓰지 않았다. 17 API 작업 중 성공12·
실패5이며 알려진 GPU 요청 단위459초(physical273/virtual slot186), NPU557초,
CPU1016 core초를 기록했다. GPU 요청 단위 합을 물리 GPU 시간으로 해석하지 않는다. 미시작 실패1건과
qualification/진단의 전체 비용은 unknown이 있어 전체 비용 완료나 성능 이득을 주장하지 않는다.
