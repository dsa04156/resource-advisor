# JCT 자원 선택 실험: 측정 결과 기록

**이번 비교도 CUDA GPU에서만 실행했다. NPU 실행·프로파일링·성능 비교는 포함하지 않았다.**

최종 상태: 사용자 요청에 따라 추가 검증을 중단하고 측정 기록만 보존했다. 최종 독립 감사·실행 후 시계 검증·통합 게이트 판정은 수행하지 않았다. 실제 Resource Advisor 스케줄링 경로에는 반영하지 않았다. 아래 수치는 실행기가 저장한 기록에서 계산한 관측값이며, 검증된 개선율이나 통계적 유의성을 주장하지 않는다.

## 실행 조건

- 기준: `profile-pool-20261008-v2`의 정책 규칙과 기존 실제 추론 워크로드를 재사용.
- 모델: 학습된 FP32 Digits MLP64→256→256→10. 고정된256개 평가 입력을 작업당16,384회 순전파(4,194,304개 반복 추론). 새로운 독립 이미지 수 또는 학습 속도 개선이 아니다.
- GPU 후보6개: K8s RTX5060Ti, GB10 Spark, RTX5080, Jetson Nano, Jetson AGX와 Slurm Orin Nano. K8s 독점 GPU3개, 공유 Jetson 각각2슬롯, Slurm GPU GRES1슬롯:명목8슬롯이며 물리 GPU8개를 의미하지 않는다.
- 도착 간격: sparse3초, moderate0.5초, burst0초. 정책별·부하별3회 반복, 회차당6작업. 총27회·162작업, 기록상162개 SUCCEEDED. 코호트가 반복 단위다.
- 순서 seed20261008; 각 회차 전에 native 자원풀을 비운다.
- Profile Only:v2 배치 규칙에 이번에 새로 측정한 고정 프로파일과 회차 내 합성 backlog를 사용. Profile + Queue도 같은 고정 프로파일을 사용하고 현재 native 큐·남은 실행·준비/재입장 시간·제출 의도를 추가한다. 원래 v2의6.2초를 재현한 결과로 해석하지 않는다.
- 본 실행 wall time:726.564초(12.11분), 완료 UTC:2026-10-08T11:01:41.960810+00:00.

## 전체 관측값

정책마다54작업·9회차. JCT는 K8s Job 생성→컨테이너 종료, Slurm Submit→allocation End 기준이다. p95는 작업 기록의 nearest rank이며, 처리량은 작업 수/회차별 native wall time 합이다. 회차 사이 drain·관찰 지연을 제외한 값이므로 전체 실행 처리량과 구분한다.

|정책|평균 JCT(s)|p95 JCT(s)|처리량(job/s)|K8s 독점 예약(s)|K8s 공유 슬롯(s)|Slurm GRES(s)|
|---|---:|---:|---:|---:|---:|---:|
|Round Robin|6.537|11.000|0.4000|62|162|72|
|Profile Only|6.389|17.000|0.3529|112|0|0|
|Profile + Queue|6.556|14.000|0.3699|108|17|78|

Profile + Queue의 관측 평균 JCT는 Round Robin 대비0.28%, Profile Only 대비2.61% 증가했다. 전체 평균 JCT 개선은 관측되지 않았다. 이 작은 차이의 방향을 타임스탬프 오차까지 포함해 확정한 것은 아니다. Queue 정책54건 중6건은 큐 상태 불확실성에 따른 명시적 Round Robin fallback이었다.

## 부하별 관측값

각 행은18작업·3회차다.

|부하|정책|평균 JCT(s)|p95 JCT(s)|처리량(job/s)|공유/독점/GRES 혼합 슬롯합(s)|
|---|---|---:|---:|---:|---:|
|sparse|Round Robin|6.722|14.000|0.2687|100|
|sparse|Profile Only|3.889|7.000|0.3529|38|
|sparse|Profile + Queue|6.444|15.000|0.2609|86|
|moderate|Round Robin|6.444|11.000|0.5294|98|
|moderate|Profile Only|7.778|17.000|0.3462|38|
|moderate|Profile + Queue|7.056|14.000|0.4286|63|
|burst|Round Robin|6.444|11.000|0.5294|98|
|burst|Profile Only|7.500|20.000|0.3600|36|
|burst|Profile + Queue|6.167|10.000|0.5143|54|

## 프로파일링·별도 파일럿 비용

- 고정 프로파일:14개 native 작업, wall44초. K8s 독점13초·K8s 공유94슬롯초·Slurm GRES17초, 혼합 합124슬롯초. 모델 학습과 공통 준비 CPU 비용은 미계측이며0으로 처리하지 않는다.
- 시간 축소 요청 전에 시작한43개 파일럿 작업은 별도 보존하고 본 비교에서 제외했다. 비용은 K8s 독점75초·공유107슬롯초·Slurm GRES41초, 혼합 합223슬롯초.
- 본 실험 비용은 표의 정책별 사용량 합으로 K8s 독점282초·공유179슬롯초·Slurm GRES150초다. 프로파일과 파일럿을 포함한 전체 기록은 독점370초·공유380슬롯초·GRES208초다. 이 단위를 더해 물리 GPU-hours·에너지·금액 절감으로 주장하지 않는다.
- 실행 중 센서 원시 시계열은 각 작업의 details.sensor와 로그에 보존했다. 연속적인 전체 풀 GPU 사용률이나 개별 작업의 센서 사용률 귀속을 확정하지 않았다.

## 보존한 산출물

- 원시 기록:`/home/jinuk/codex-work/mlops/.state/resource-advisor/jct-profile-queue-20261008-short-v1` (capture.json, jobs/, logs/, manifests/, choices/, snapshots/, profiles/, 실행 소스·사전 등록·변경 사유).
- 측정 요약:`recorded-results.json`; 작업별 CSV:`recorded-main.csv`; 회차별 CSV:`recorded-cohorts.csv`.
- 검증 중단 상태:`verification-stopped.json`.
- 별도 파일럿:`/home/jinuk/codex-work/mlops/.state/resource-advisor/jct-profile-queue-20261008-v1` 및 pilot-reconciliation.json.
- 실험 코드 브랜치:`feat/jct-aware-profile-queue`; 실제 실행 코드 commitf3ac72d39771fe1bbe6563e9b351a04eeccfa25c는 executed-source/와 execution-provenance.json에 보존했다.

## NAIS 활용 범위

검증된 신규 개선율은 기재하지 않는다. 사용 가능한 사실 중심 문장:

> GPU별 실측 실행시간에 큐 상태와 준비·대기시간을 결합한 JCT-aware 선택 정책을 구현하고, 이기종 GPU6개에서3종 부하·3회 반복·162개 실제 추론 작업을 비교했습니다. 평균 JCT 개선이 관측되지 않아 실제 스케줄러 반영을 보류하고, 원시 실행 기록과 정책 선택 근거를 보존했습니다.

리뷰에서 저장된 큐 입력과 원시 native demand의 대조를 더 강화해야 한다는1건을 확인했다. 사용자 요청으로 후속 검증을 중단했으므로 이 항목과 최종 감사는 미완료 상태로 기록한다.
