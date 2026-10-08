# 동일 AI 워크로드 프로파일 재사용: GPU·NPU 재실험

2026-10-08, `profile-all-20261008-v3`. **현재 연결된 가속기 11개에서 실제 AI 작업을 실행했고 본 작업 22/22개가 성공했다.** 동일 Digits MLP 비교는 GPU6개와 Intel NPU1개에서 정책별7작업·코호트1회다. 나머지 NPU4개는 기존 모델을 별도 그룹으로 실행했다. 모든 소유 작업은 종료하고 native 큐 해제를 확인했다.

동일 MLP의 동기화 추론 구간 합산은 **25.961240→5.838880초, 77.51% 감소**했다. 그러나 평균 JCT는 **5.86→18.71초**, 처리량은 **0.700→0.219 jobs/s**로 악화됐다. 프로파일 재사용 정책을 운영 스케줄링에 통합하지 않았다. 통계적 개선·물리 비용 절감·전체 가속기 공통 모델 지원을 주장하지 않는다.

## 실제 사용한 장비와 모델

| 그룹 | 실제 장비 | 고정 모델·입력·작업량 | 본 작업 |
|---|---|---|---:|
| 동일 MLP | RTX5060Ti·RTX5080·GB10·Orin Nano·AGX Orin·Slurm Orin Nano·Intel AI Boost NPU | 학습된 Digits MLP64→256→256→10, held-out256개, 16,384 forwards/job | RR7 + Reuse7 |
| Hailo | Hailo8 두 장치 | 동일 ResNet50 HEF와 고정100개 입력/job | RR2 + Reuse2 |
| Mobilint | ARIES 한 장치 | 기존 Candy MXQ, 고정 입력10회/job | RR1 + Reuse1 |
| Rockchip | RK3399Pro 한 장치 | 공식 ResNet18 RKNN, 고정 단일 입력10회/job | RR1 + Reuse1 |

GPU6개·NPU5개는 물리 가속기 개수다. Intel NPU는 RTX5080과 같은 호스트에 있어 가속기11개가 호스트11대를 뜻하지 않는다. Jetson의 shared slot도 물리 GPU 여러 개로 세지 않았다. 현재 등록된 Slurm 가속기는 Orin GPU 하나이며, 등록되지 않은 DEEPX/AMD 실행 경로를 사용했다고 주장하지 않는다.

Hailo는 HailoRT만 있고 DFC가 없었고, Mobilint는 qbruntime만 있고 qb Compiler/Quiler가 없었다. Rockchip은 기존 RKNN Lite1.7.1과 compiled ResNet18만 확인했다. 새 SDK 확인 Job은 Python3.7의 `importlib.metadata` 부재로 실패했고 추론은 수행하지 않았다. 이는 조사한 설치에서 **동일 MLP 컴파일 도구·산출물이 부족했다는 뜻**이며 하드웨어가 MLP를 지원하지 않는다는 증명은 아니다. [도구 확인 근거](evidence/profile-all-20261008-v3/toolchain-availability.md)에 조사 범위와 공식 문서를 기록했다. 서로 다른 NPU 모델을 동일 MLP 순위에 섞지 않았다.

## 동일 MLP 비교 조건과 결과

첫 실험의 fixture SHA `644a0c537bd997d18ce29e22ca5074ba5d8dccf340b8b39a2fb435de2113ba25`를 그대로 썼다. 작업마다 동일256개 입력을 반복해4,194,304회 추론했다. 새로운 독립 이미지419만 개가 아니다. 모든14개 본 작업은 CPU 기준 top1과100% 일치하고 held-out 정확도98.046875%를 유지했다. 고정 numerical gate는 atol0.003·rtol0.001이다.

GPU는 기존 CUDA FP32 경로를 재사용했다. Intel은 같은 FP32 weights·입력·논리적 MLP를 OpenVINO2026.3.1의 static MatMul/Add/ReLU로 실행했다. 직접 `NPU` 컴파일과 `execution_devices=["NPU"]`를 확인했고 CPU/AUTO/HETERO fallback을 허용하지 않았다. Intel 하드웨어 내부 FP16은 공개하며 동일 numerical gate를 적용했다. 본 실행 Intel 컴파일은0.095449초, 추론 구간은3.633663초였다. 컴파일 캐시 이력은 확정하지 않았다.

RR은 후보7개를 한 번씩 사용했다. Reuse는 후보별8-forward native 측정1회의 추론 시간을16,384회로 선형 예측하고 관측 시작/종료 overhead와 정적 코호트 backlog를 더하는 첫 실험의 정책이다. 프로파일과 전체 배정을 본 작업 전에 고정했다. seed20261008의 실행 순서는 RR→Reuse이며 Reuse는 RTX5080에3개, RTX5060Ti와GB10에각2개를 배정했다. Intel과 모든 Jetson은 profiling과 RR에서 실제 실행했다. live queue 기반 JCT 정책을 이번 비교에 넣지 않았다.

두 조건은 같은 후보 풀·모델·입력·작업량의 burst 요청이다. native 제출을 동시에 시작했으나 실제 수락시각 분산은 RR0초·Reuse1초로 정확히 같지 않다. GPU는 기존 Kueue queue, Slurm은 ra-lab/ra-normal·compute·GPU GRES를 사용했다. GPU 요청 CPU1·512Mi·slot1, Intel은 기존 qualified CPU1·1Gi·NPU1 요청을 유지했다.

| 측정값 | Round Robin | Profile Reuse |
|---|---:|---:|
| 성공 본 작업 |7/7|7/7|
| 동기화 추론 구간 합산 |25.961240초|5.838880초|
| 평균 JCT |5.857143초|18.714286초|
| p95 JCT |10초|31초|
| 평균 대기 |0.714286초|15.571429초|
| 코호트 완료시간 |10초|32초|
| 처리량 |0.700 jobs/s|0.218750 jobs/s|
| Kubernetes exclusive GPU 할당 |6초|22초|
| Kubernetes GPU shared-slot 할당 |17초|0초|
| Slurm GPU GRES 할당 |8초|0초|
| Intel NPU 할당 |5초|0초|

평균 JCT는219.51% 증가했고 처리량은68.75% 감소했다. RR은 장비별로 분산했고 Reuse는 빠른 GPU3개에 집중했다. 실제 후속 RTX5080 작업은19초·29초 대기했다. 평균 대기 증가14.86초가 평균 할당 이후 구간 감소2.0초를 상쇄했다. 이는 기록된 시간 분해이며 특정 Kueue controller 내부 지연의 인과 기여율까지 확인한 실험은 아니다.

JCT는 Kubernetes Job 생성→container 종료, Slurm Submit→End다. 대기 경계는 각각 PodScheduled/Slurm Start이며 표의 할당도 그 시점부터 종료까지다. native 시각은1초 해상도다. Slurm controller·worker의 사전 NTP/UTC 및 로컬 왕복 시각을 기록했으나 전체 NPU 호스트의 정밀 시계 오차까지 재검증하지 않았다. p95는7개 중 최댓값이며 조건별 정책 표본은n=1이다. 연속 전체 풀 활용률·전력·물리 GPU/NPU-hours는 미측정이다. 서로 다른 센서·shared slot·GPU/NPU 예약 단위를 합쳐 경제적 절감율을 만들지 않는다.

## 별도 NPU 그룹

| 그룹 | 평균 JCT RR→Reuse | 추론 구간 합산 RR→Reuse | 해석 |
|---|---:|---:|---|
| Hailo ResNet50 |3→2초|0.679083→0.678960초|두 정책 모두각장치1개 선택. 배치 개선 입증 없음|
| Mobilint Candy |1→2초|0.261905→0.260367초|후보1개. 배치 최적화 개선율 없음|
| Rockchip ResNet18 |12→12초|0.128419→0.175469초|후보1개. 배치 최적화 개선율 없음|

Hailo는 고정100개 입력에서 정확도80%, CPU reference agreement97%로 기존 품질 계약을 통과했다. Mobilint는 고정 출력 재현·형태·유한값을, Rockchip은 기준 class812를10/10회 확인했다. 이를 Mobilint 이미지 변환의 의미적 정확도 또는 Rockchip 전체 데이터셋 정확도100%로 해석하지 않는다. Rockchip은 기존 trusted lab host bridge를 재사용했으며 다중 사용자 격리를 새로 검증하지 않았다. firmware fingerprint가 불완전한 경로의 운영 자동 재사용 gate는 유지했다.

각 그룹도 코호트1회이며 몇 초의 native 차이에는1초 양자화와 시계 한계가 있다. Hailo33.33% JCT 개선 같은 수치를 검증 성과로 사용하지 않는다.

## 초기 비용과 기록 정정

동일 MLP의 성공 microprofile7개 예약은 Kubernetes exclusive GPU5초, shared slot5초, Slurm GRES0초, Intel NPU1초로 기록됐다. Slurm0초는 양자화된 관측값이며 물리 비용0의 증명이 아니다. 첫 Intel qualification은 EXECUTION_DEVICES 문자열 ABI 처리 오류로 실패해NPU 예약16초·JCT17초를 썼다. 기존 normalization과 회귀 테스트로 수정한 새 불변 source에서 성공했으며 실패를 삭제하지 않았다. Intel profiling 예약은 실패까지17초다. CPU 학습·공통 준비 비용은 미측정이다.

별도 NPU4개 profiling과8개 main 및 SDK 확인3개도 전부 보존했다. NPU recorder가 처음에 container-running 기간을 예약량으로 표기한 오류는 리뷰에서 발견했다. **PodScheduled→종료 예약과 container-running 기간을 분리해 저장된 native 영수증으로 정정했고 하드웨어를 재실행하지 않았다.** 실행 당시 capture·frozen profiles·배정·초기 출력은 그대로 남겼다. [정정 근거](evidence/profile-all-20261008-v3/npu-metric-correction.json)와 [정정 요약](evidence/profile-all-20261008-v3/npu-corrected-summary.json)을 사용한다. 정정 후 별도 NPU4개 profiling 예약은16 device-seconds, 본 작업은36 device-seconds다. Hailo 본 작업 예약 RR6→Reuse4초, Mobilint1→1초, Rockchip12→12초다. 초기 container 기간 합46초와 정정 예약 합52초를 구분하며 pre-PodScheduled quota 보유와 물리 활동량은 포함하지 않는다. 초기 container 기간으로 비용 회수나 물리 절감 주장을 하지 않는다.

SDK 확인의 Hailo/Mobilint 성공과 Rockchip 실패를 포함해 전체 native 시도는37개다. 프로파일/준비15개 중 성공13·실패2, 본 작업22개 모두 성공했다. 장치 초기화·firmware·전역 queue 설정·운영 스케줄링 경로는 바꾸지 않았다.

## 원시 자료와 재현

[MLP CSV](evidence/profile-all-20261008-v3/mlp-main.csv), [MLP 요약](evidence/profile-all-20261008-v3/mlp-summary.json), [산술 재계산](evidence/profile-all-20261008-v3/recompute.py), [공개 native 증거 압축본](evidence/profile-all-20261008-v3/native-records-public.tar.gz), [원본·공개본 SHA manifest](evidence/profile-all-20261008-v3/artifact-manifest.json)을 보존했다.

완전한 원시 원본은 공유 작업공간 `.state/resource-advisor/profile-all-20261008-v3/`에 있다. 저장소의 private 설정 규칙에 따라 Git 공개본은 노드명·private registry/주소를 익명화하고 다른 작업의 Pod 설정과 compiled blob을 제외했다. 원본과 공개본의 hash를 구분한다. 모델·입력·수치·native 시각·소유 Job/Pod 관계는 남겨 오프라인 산술 재계산이 가능하다. 공개 원시 영수증의 native 시작/종료 시각, MLP 작업별16,384개 측정과 품질, NPU12개 영수증의 정정 예약량을 읽어 CSV·요약과 일치하는 것을 재계산했다. 재계산에는 가속기 제출이 없다.

```sh
rtk proxy python3 docs/evidence/profile-all-20261008-v3/recompute.py
```

새 native 실행에는 새로운 빈 directory와 private site 설정이 필요하다. archived plan의 후보·source·qualified fixture·8-round profiling을 준비한 뒤 `examples/run_profile_all.py --directory NEW --private-remote-module PRIVATE`를 사용한다. 별도 NPU 그룹은 `examples/run_npu_profile_reuse.py --directory NEW --artifacts PRIVATE_ARTIFACTS`다. 기존 capture가 있으면 재제출을 거절한다. 실행 당시 driver/source와 이후 예약 계측 수정의 digest를 구분한다.

새 구현 병합 후 전체 소프트웨어 테스트1332개 통과·4개 skip·기존 경고3개를 기록했다. 계측 수정 후 관련8개 테스트와 Ruff도 통과했다. 두 축 코드 리뷰의 private hostname·NPU 예약 경계 문제를 수정했다. 이전 JCT 실험의 사용자가 중단한 최종 감사를 재개하거나 해당 결과의 운영 gate가 통과했다고 주장하지 않는다.

## NAIS 자기소개서에 쓸 수 있는 표현

> “GPU6개와 Intel NPU로 구성한 이기종 자원풀에서 동일 학습 모델·입력의14개 본 작업을 비교했습니다. 실측 프로파일 재사용으로 단일 코호트의 동기화 추론 구간 합산을25.96초에서5.84초로77.51% 줄였고, 완료시간이5.86초에서18.71초로 악화되는 큐 집중 한계를 확인해 운영 적용을 보류했습니다.”

장비 커버리지는 **가속기11개·본 작업22/22 성공**으로 표현할 수 있지만 모델별 그룹을 함께 밝혀야 한다. 기존5GPU 실험의72.25%와 이번7후보 결과의77.51%는 자원풀·baseline이 달라 직접적인 개선 비교가 아니다. NPU 전체를 동일 MLP로 비교했다고 쓰거나 이번 관측을 반복 검증된 성능 개선으로 확대하지 않는다.
