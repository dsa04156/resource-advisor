# 동일 AI 워크로드 프로파일 재사용: 단일 실행 결과

**실험 범위는 CUDA GPU 추론이다. NPU에서는 이 워크로드를 실행하거나 프로파일 재사용 효과를 측정하지 않았다. 아래 개선율과 슬롯 할당 기록은 GPU 실험에만 해당한다.**

2026-10-08. 사용자 요청에 따라 **조건별 코호트1회**, 각5개 native Job만 실행했다. 본 실행10개 모두 성공했다. 짧은 측정5개와 실패한 GB10 runtime 시도1개를 포함해 전체16개 중 성공15개·실패1개다. 현재 실행 중인 소유 Job은0개다. 추가 반복은 수행하지 않았다.

## 비교 조건과 실제 실행 범위

실제 손글씨 데이터로 CPU에서 학습한 **MLP 64→256→256→10**을 사용했다. Train/test를 seed20261008로 층화80/20 분리하고, 고정한 held-out 이미지256개에서 정확도 **98.046875%**였다. 동일 FP32 weights·입력·CUDA PTX를 RTX5060Ti, RTX5080, GB10, Orin Nano, AGX Orin에 적용했다. 모든 본 실행은16,384 forwards×256 images로 작업량4,194,304회를 동일하게 맞췄다. 이 숫자는256개 입력의 반복 추론 수이며 새로운 독립 이미지 수가 아니다.

Kubernetes/Kueue의 기존 `ra-f0-gpu` queue, normal priority, CPU1·memory512Mi·accelerator slot1을 사용했다. GPU3개는 exclusive resource, Jetson2개는 각각 shared slot을 사용했다. GPU 물리노드5대와 nominal slot7개를 구분한다. [마지막 allocatable/queue readback](evidence/profile-pool-20261008-v2-results/pool-readback.json)은 별도로 보존했다. 전후 전체 인프라 digest 일치까지 검증했다고 주장하지 않는다.

Baseline은 측정값을 읽지 않는 물리노드 round robin, 프로파일 조건은 GPU별8-forward 측정1회의 시간·관측 startup을 고정해 예상 완료시간이 가장 작은 후보를 선택한 **실험용 dispatcher**다. `resource-advisor`의 CUDA driver 실행 경로와 기존 결과 감사 코드를 재사용했다. 현재 production `compile_plan`을 성능 profile/JCT 정책으로 변경하거나 API·SQL·MLflow 통합을 새로 검증한 실험은 아니다.

Profiling 후 seeded arm 순서는 Baseline→Reuse였다. 각 조건은즉시5개 요청 burst이며 관측 native 생성시각의 분산은 Baseline0초·Reuse1초다. 두 조건의 정확한 native 도착시각까지 같지는 않다. 후보pool·모델·입력·작업량·자원 요청은 같다. Reuse는 RTX5080 두 작업, GB10 두 작업, RTX5060Ti 한 작업을 선택했다. 두 Jetson도 profiling과 Baseline에서 실제 실행됐다.

Slurm compute host2개는 SSH로 접근 가능했으나 컨트롤러 접속은 `No route to host`, compute node의 `squeue`도 timeout이었다. **Slurm Job은0개**다. Kubernetes·Slurm 전체 풀의 완성된 비교로 인용하지 않는다.

## 측정 결과

JCT는 native Job 생성→container 종료, 대기는 Job 생성→PodScheduled, 할당량은 PodScheduled→container 종료다. Native 시각 해상도는1초다. FP32 추론 실행구간은 host launch와 CUDA synchronize를 포함한 고해상도 측정이며 순수 커널 시간 또는 JCT가 아니다.

| 지표 | Baseline | Profile reuse | Baseline 대비 관측 변화 |
|---|---:|---:|---:|
| 본 실행 동기화 추론 구간 합산 | 14.967436초 | 4.153377초 | **72.25% 감소** |
| 평균 JCT |6.0초|6.2초|3.33% 증가|
| p95 JCT |11초|12초|9.09% 증가|
| 평균 대기 |1.0초|4.2초|320% 증가|
| 본 실행 accelerator-slot 할당시간 |25초|10초|**60% 감소**|
| 본 실행 accelerator-slot hours |0.006944|0.002778|60% 감소|
| 물리 GPU-hours |unknown|unknown|exclusive/shared를 합쳐 확정하지 않음|
| 코호트 완료시간 |11초|13초|18.18% 증가|
| 처리량 |1,636.36 jobs/hour|1,384.62 jobs/hour|15.38% 감소|
| 같은 입력 반복 추론 처리량 |1,906,501.82 images/s|1,613,193.85 images/s|15.38% 감소|
| nominal slot 점유율 |32.47%|10.99%|할당slot시간/(nominal7slots×cohort wall)|
| 연속 전체 풀 GPU 사용률 |unknown|unknown|유효한 연속 측정 없음|

NVML과 Jetson sysfs load는 원시 데이터와 summary의 `sensors_by_native_job`에 종류·노드·관측 시간과 함께 보존했다. 서로 다른 센서와 선택 장비를 섞어 활용률 개선율을 만들지 않는다. 짧은 NVML 창의 값도 전체 풀 평균이나 정확한 작업 기여율로 해석하지 않는다.

정책 수준 표본은**n=1/조건**이다. p95는각5개 Job 중 최댓값이다. 통계적 유의성·반복성·다른 모델에 대한 일반화는 검증되지 않았다.

## 초기 비용·순효과·회수 시점

성공한5개 짧은 측정과 실패한 GB10 시도를 모두 포함한 초기 native 할당 proxy는 **10 slot-seconds**, 최초 Job 생성부터 마지막 측정 종료까지 wall은 **117초**다. GB10에 필요한 기존 `nvidia-spark` RuntimeClass를 빠뜨린 실패와 수정 대기시간도 유지했다. CPU 학습/fixture 준비는 공통 준비이며 소요 비용은unknown으로 보존한다.

본 실행 포함 Reuse 총할당은 **20 slot-seconds**로 Baseline25 대비 산술상20% 작다. 첫 관측 코호트인5작업에서 이 proxy가 Baseline을 밑돌았다. 다만 순차이는5초이고 총16개 짧은 native 시각의1초 해상도에 따른 차이 bound는약±16초다. 공유슬롯도 포함하므로 **물리 GPU 비용 절감과 정확한 비용 회수 시점은 확립되지 않았다. 이20%를 자기소개서 성과에서 제외한다.**

초기wall을 포함하면 Reuse130초 vs Baseline11초로 비용을 회수하지 못했다. 이117초에는 실패·수정 간격이 포함돼 있다. 자동 profiler의 순수 실행시간으로 바꾸거나 실패를 지워 유리한 수치를 만들지 않았다. 미실행 사용 횟수까지 회수곡선을 외삽하지 않았다.

## JCT가 개선되지 않은 원인과 확인 근거

프로파일이 빠른 GPU를 골랐지만, 같은 GPU의 후속 작업은native 재입장·자원 대기를 겪었다. Reuse의 후속 RTX5080/GB10 작업은대기7초/10초였다. 첫 작업들의 대기는1초/2초였고, Baseline은 모든 노드에 한 작업씩 배정해각1초였다. 개별 추론 실행이 짧아 재입장 대기가 연산시간 절감을 상쇄했다. [native Job/Pod 원시 기록](evidence/profile-pool-20261008-v2-results/capture.json)과 [Kueue Workload16개](evidence/profile-pool-20261008-v2-results/kueue-workloads.json)를 보존했다. 이 근거는 대기 경로를 확인하며, 특정 controller 내부 지연의 기여율까지 분해한 실험은 아니다.

추가 반복 없이 종료하라는 최신 요청에 따라 후속 GPU 실행을 하지 않았다. 후속 실험이 필요하면 queue 재입장 비용을 넣은 배치·배정 정책과 Slurm controller 복구를 먼저 준비해야 한다. 현재 결과는GPU 연산구간/할당 proxy 감소와 JCT·처리량 악화를 함께 보여준다.

## 재현과 원시 자료

[CSV](evidence/profile-pool-20261008-v2-results/main.csv), [audited summary](evidence/profile-pool-20261008-v2-results/summary.json), [raw capture](evidence/profile-pool-20261008-v2-results/capture.json), [immutable source/fixture](evidence/profile-pool-20261008-v2-results/source.json), [SHA-256 manifest](evidence/profile-pool-20261008-v2-results/manifest.json)를 보존했다. 실행한 코드는source.json 안의workload.py 및 `frozen-workload.py.txt`다. 현재 파일의 lint/format 수정과 실행 당시 frozen source를 구분한다.

저장소 루트에서 새 출력 경로로 오프라인 재계산한다. 이 명령은GPU 작업을 제출하지 않는다.

```sh
rtk proxy uv run python examples/analyze_pool_inference.py \
  --directory docs/evidence/profile-pool-20261008-v2-results \
  --output /tmp/resource-advisor-pool-recheck
```

새 실제 실행에는새 ID가 필요하다. archived plan/source의5개 노드·동일queue·runtimeClass·자원을 사용해 먼저1회microprofile을 만들고, `examples/run_pool_inference.py --directory NEW_PRIVATE_DIRECTORY`로각5개 작업을 한 번씩 실행한다. 기존capture가 있으면 replay를 거절한다. Fixture를 다시 학습할 경우scikit-learn1.9.1/numpy2.5.3/threadpoolctl3.7.0, seed·split·60iterations·singleCPUthread를 유지하고 archived fixture hash와 대조한다. 새 fixture hash가 다르면기존 profile을 재사용하지 않는다.

기존 감사 회귀11개와ruff/format/compile 검사를 통과했다. Spec 검토는 source·fixture·kernel·작업량·노드/runtime/resource 일치와 누출없음을 독립 확인했다. Standards 검토에서 지적한센서 혼합·slot/physicalGPU 구분·초 단위 순효과 한계를 반영했다. 저장소의다른 변경이나 productionscheduler 설정은 수정하지 않았다.

## NAIS 자기소개서에 사용할 정량 성과3개

모두이번 단일 코호트의 lab 검증 범위로 한정한다. Slurm 포함성과, 전체JCT개선 또는확정적인경제적절감으로 확대하지 않는다.

1. **동기화 추론 실행구간 합산72.25% 감소** — “실제 학습된 분류모델의 동일 입력·작업량을 이기종 GPU 풀에서 비교하고, 실측 프로파일을 활용한 배정으로 단일 검증 코호트의 동기화 추론 실행구간 합산을14.97초에서4.15초로72.25% 줄였습니다.”
2. **본 실행 accelerator-slot 할당시간60% 감소** — “같은5개 작업의 프로파일 미사용·사용을 비교해 본 실행의 네이티브 가속기 슬롯 할당 기록을25초에서10초로60% 줄였으며, 초기 비용과 공유슬롯·계측 해상도를 별도로 감사했습니다.”
3. **GPU노드5대·본 작업10/10 성공·기준출력100% 일치** — “Kubernetes GPU노드5대에서 같은FP32 모델과 입력을 검증하고, 본 작업10개 전부를 성공시켜CPU 기준출력과100%의 분류 일치율을 확인했습니다.”

자기소개서에서는실험 범위를함께 쓰며, 질문받으면평균JCT6.0→6.2초와처리량15.38% 감소도 설명할 수 있어야 한다.
