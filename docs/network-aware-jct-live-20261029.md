# Network-aware JCT: 실제 장비 검증과 LAN 배포 (2026-10-29)

포털: http://10.254.192.47:18420/console#training

MobileNetV3-small을 선택하면 성공 실행이 가장 많은 등록 학습 조건을 기본으로 연다. 단일 GPU와 2-GPU DDP 조합의 예상 JCT를 보여주며, `GPU+Mobilint KD` 조건에서 공동 작업의 예상/실측을 조회한다. `JCT 비교 기록`에서는 정책별 고정 예측, 실제 JCT, 오차, 작업 상세 및 MLflow를 확인한다. Mobilint 공동 작업의 일반 제출 버튼은 보조 장치 예약을 확인하지 못하면 차단한다. 재실행에는 기존 교사 준비/예약 CLI 경로가 필요하다.

## 실행 전에 고정한 예측의 검증

| 경로 | 예측 | 이후 실제 JCT | 절대오차 | 실제 검증 작업 |
|---|---:|---:|---:|---|
| Spark GB10 단독 | 9 s | 9 s | 0 s | `j-2874d96a58044f1592971597a758a50e` |
| RTX 5060 Ti + RTX 5080, DDP | 33 s | 33 s | 0 s | `j-78f1b227314d4dbdab2d9b08e36e70f9` |
| Spark GB10 + Mobilint ARIES2 교사 | 17 s | 16 s | 1 s | `j-82f45b38acfe4e4c852e1ffbb3e49998` |

세 작업 모두 SUCCEEDED. 예측은 앞선 별도 보정 실행으로 만들고, 검증 작업의 네이티브 제출 전에 저장했다. 검증 결과를 자기 예측으로 재사용하지 않았다. PostgreSQL `jct_outcome`과 MLflow experiment 3에서 동일한 예측·실측·오차를 읽어 대조했다. 네이티브 JCT 경계는 `scheduler_submitted_at → backend_finished_at`이다. API 요청부터 네이티브 제출까지의 시간은 이 JCT에 포함하지 않는다.

MLflow run IDs: 단독 `8e97d653f4fe48d597879bd2651d608f`, DDP `496554be905449e2aa858cd38eee5c6b`, 공동 `8d32cdcf594d4b0d9c04bc1d42b43e45`.

## 후보별 보정 결과

동일 MobileNetV3-small 초기 파라미터, 10-class ImageNetV2 subset, AdamW, fp32, 전역 배치 16, 50 step, seed 20261029. DDP는 각 GPU 배치 8, SyncBatchNorm, NCCL이다. KD는 다른 학습 목적이므로 단독 CE와 같은 순위 실험으로 합치지 않는다.

| 조합 | 관측 유휴 상태에서 예상 JCT | 실제 데이터 전송 |
|---|---:|---:|
| RTX 5060 Ti ×1 | 12 s | 0.085 s |
| RTX 5080 ×1 | 22 s | 0.164 s |
| Orin ×1 | 58 s | 1.136 s |
| Spark GB10 ×1 | 9 s | 0.181 s |
| RTX 5060 Ti + RTX 5080 | 33 s | 각 rank 0.065 / 0.169 s |
| Spark + Mobilint 교사 | 17 s | 0.168 s |

실제 전송 객체는 16,558,080 bytes의 tar, SHA-256 `64dfa50a05062cd99dc781d9df041f690fb154ec2f4cce60a71caab67d8093f1`. ser0001 HTTP 원본을 각 작업 Pod가 직접 받고 검증했다. 캐시는 `/tmp/ra-stage`의 작업별 임시 저장소로 cold 상태였다. 호스트에서 대신 측정한 값을 원격 노드 성능으로 쓰지 않았다. 보정 프로파일은 한 건씩이며, 후보별 숫자는 보편적인 장치 성능이 아니다. Orin 58초에는 30초 런타임 시작/이미지 레이어 준비가 포함된다.

DDP는 두 Pod의 UID/노드와 각자의 receipt를 검증한다. 네트워크 대체 시 각 rank의 전송+압축 해제 합을 유지하며, 동기화된 경로의 최댓값을 사용한다. NCCL은 연산 시간 안에 한 번 포함된다. 공동 작업은 GPU가 학습하고 Mobilint가 frozen teacher forward 50회를 수행했다. 교사 RPC 약 7.3초와 NPU 추론 약 1.8초는 학습 루프 내부의 중첩 구간으로, 데이터셋 전송에 다시 더하지 않는다. NPU 역전파나 원자적 GPU/NPU 공동 예약을 구현했다고 주장하지 않는다.

## 정책 비교의 의미

`vt-final-static-20261029`, `vt-final-profile-only-20261029`, `vt-final-profile-queue-20261029`, `vt-final-profile-queue-network-20261029`를 같은 후보 풀에서 고정했다. 비교 ref는 `vt-final-comparison-{single,ddp,joint}-20261029`다. 사용자 요청에 따라 반복 캠페인 없이 네 정책의 shadow 선택을 기록하고 D 경로만 한 번씩 검증했다. Static은 고정 후보이며 자체 JCT 예측값은 없다. 유휴 단독 비교에서 B/C/D 모두 GB10 9초를 선택했다.

따라서 이번 결과는 구현과 실제 실행 연결의 검증이며, D가 A/B/C보다 빠르다는 증거가 아니다. S2 혼잡 반복, S3 10/20/100Mbps 제어, S4 원본 위치 변화, S5 cold/warm 모델 비교, 평균/p95/Goodput의 유의성 검증은 이번 추가 실행에 포함하지 않았다. n=1의 통계적 변동성은 unknown이며 신뢰구간을 만들지 않는다. 네이티브 시각 해상도는 1초라 0초 오차도 정밀한 정확도 증명이 아니다.

## 구현·운영

기존 Profile/Queue 및 Kubernetes/Slurm 경로를 유지했다. 측정 collector와 NetworkProfile, 데이터 위치 및 캐시 바인딩, 다중 GPU 비용 계산, 실제 per-rank 검증, PostgreSQL/MLflow 기록을 연결했다. 큐 관측기는 30초 간격으로 실제 점유를 읽으며, 다중 단위 점유·다른 route alias·미확인 점유는 근거 없이 빈 자원으로 보지 않는다. 최신 큐 조회용 `ix_ra_route_queue_latest`를 CONCURRENTLY 생성했고 valid/ready 및 Index Scan을 확인했다. 이번 신규 native 실행은 Kubernetes에서 수행했으며 새 Slurm 장비 실험을 했다고 주장하지 않는다.

LAN API: `console-api@sha256:4c7b43b018323d731b9ff067f4287f41fa1df5f081bc4c2e2de8f805be74464d`.
Worker/observer: `console-api@sha256:e32472ac64cd9346085a04e2267456a0e903241ace58dcd354bb3d320e39c063`.
이미지 주소 앞부분은 `10.254.192.47:5000/resource-advisor/`다. 기존 live 기능을 보존한 composite에 변경 파일을 추가했다. API는 [image-only patch](../deploy/vision-training/api-image-patch.yaml), 작업 경로는 [worker](../deploy/vision-training/worker.yaml), [queue observer](../deploy/vision-training/queue-observer.yaml), [DDP route](../deploy/vision-training/ddp-amd64.yaml), [RBAC](../deploy/vision-training/ddp-rbac.yaml), [dataset source](../deploy/vision-training/dataset-source.yaml)로 기록했다.

검증한 vision 이미지: amd64 `b2d92c21d4658210efc996f310c31f2db06df9f4f33c6939881b9797be4549f7`, arm64 `a548d03c632d4c2be2908fe4ff7de0ccd347dfdf4f7b83745ff54fda19ee1a0a`. 이 불변 이미지의 데이터 소스는 HTTP로 한정한다. HTTPS watchdog 수정은 소스/API 패키지와 테스트에 반영됐지만, 성공한 HTTP 보정 실행을 반복하지 않기 위해 위 vision 이미지는 바꾸지 않았다. AGX는 NotReady여서 새 측정이 없다.

Mobilint 교사 Pod `ra-vt-kd-teacher-20261029b`(UID `3d66baf4-13ae-4109-9055-3024dc5f1ddd`)는 기존 7200초 deadline인 2026-10-29 23:59:23 KST까지 한시 예약 상태다. 기존 타 실험은 중단하지 않았다. 실험용 대용량 파일은 `/mnt/data3tb/user-storage/jinuk/`에 두었다.

## 확인 근거

- 전체 통합 테스트: 1,759 passed / 18 skipped. 이후 변경 범위 회귀: 251 passed / 7 skipped, 67 passed, 최종 통합 변경 186 passed. 기본 조건 선택 소규모 수정: 30 passed. 서로 겹치는 테스트이므로 합산한 고유 테스트 수가 아니다.
- 독립 Standards/Spec 리뷰 2회 후 발견한 receipt, TLS, queue 및 per-member 시간 합산 결함을 수정하고 관련 회귀를 확인했다.
- LAN API/worker/observer rollout, PostgreSQL/MLflow 세 지표 대조, 1440/390px 화면과 페이지 가로 넘침 없음을 확인했다.
- [원시 측정·고정 예측·실측·오차 JSON](evidence/network-aware-jct-20261029/results.json)
- [단일 후보 화면](evidence/network-aware-jct-20261029/final-training-1440.png), [2-GPU 화면](evidence/network-aware-jct-20261029/final-ddp-1440.png), [GPU+Mobilint 결과](evidence/network-aware-jct-20261029/final-joint-1440.png), [모바일](evidence/network-aware-jct-20261029/final-training-390.png)

- [정책 비교: 고정 예측과 실제 오차](evidence/network-aware-jct-20261029/final-policy-comparison-1440.png)
