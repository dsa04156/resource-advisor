# M1 CPU → GPU 계산 — 사전 고정 계획

**아직 실행하지 않은 수용시험 계획이다.** 기존 CPU-only launcher의 자원
요청을 CPU 계산 결과로 간주하지 않고, 서로 다른 실제 계산 Job 두 개를
캐시 없는 한 KFP workflow에서 순서대로 실행한다.

- CPU: Linux amd64, scalar Python FP64 64×64 행렬 계산, seed0, warmup1,
  timed iterations5. 각 원소를 별도 닫힌식으로 검사해 numerical agreement1.0을
  요구한다. GPU runtime을 import하지 않으며 명시적 GPU visibility mask를
  사용한다. 요청 CPU1/512MiB, 가속기0, 실제 host RSS peak ≤256MiB.
- GPU: 이미 구현된 PyTorch CUDA FP32 256×256 matmul, seed0, warmup3,
  iterations20, GPU1/CPU1/2GiB, numerical agreement≥0.99. 기존 qualified
  PyTorch runtime과 source를 그대로 사용하며 CUDA fallback을 허용하지 않는다.
- 실행 전 CPU와 GPU 각각 **한 개의 실제 qualification Job**을 사용한다.
  새 관측을 근거로 새로운 capability/variant/workload 식별자를 등록하며,
  만료된 예전 capability의 timestamp를 고쳐 쓰지 않는다.
- CPU 모델은 qualification에서 실제 `/proc/cpuinfo`로 조사한다. 장비가
  아직 측정되지 않았다는 사실을 감추거나 launcher의 CPU 모델을 재사용하지 않는다.
- 이후 KFP는 CPU Compute API Job 성공 뒤 GPU Job을 제출한다. 두 단계의
  run key는 서로 다르고 캐시를 끈다. 이것은 실행 순서 의존성이며 데이터셋
  전달이나 모델 학습 pipeline의 근거가 아니다.
- 전체 **최대 native 계산 Job4개**, 자동 retry/대체 trial0개. 각 native
  active deadline≤90초, queue deadline≤120초, collection≤180초,
  launcher owner lease60초. KFP controller/launcher 자체 Pod는 계산 Job 수와
  별도로 기록하며 GPU를 요청하지 않는다.
- 기존 allowed node, LocalQueue/ClusterQueue·쿼터, 드라이버·런타임 버전 유지.
  worker만 동일 lock의 source image로 업데이트하여 CPU-only 예약의 가속기
  수0을 관측 requests에서 확인한다. GPU/NPU 요청 누락은 계속 unknown이다.
- 프로파일링/추천/성능 향상 비교는 이 시험의 목적이 아니다.

## 확인할 근거

1. 명령·source SHA·digest-pinned image·native manifest를 제출 전에 비공개
   파일로 고정한다. 공개 source commit의 CI 성공을 확인한다.
2. CPU qualification의 실제 CPU/Python/노드·zero accelerator requests,
   GPU qualification의 실제 CUDA/device allocation과 결과 identity/digest를 검사한다.
3. KFP uncached task 상태·순서와 distinct ComputeJob/Attempt/native UID를
   연결한다. CPU가 실패하면 GPU 단계가 실행되면 안 된다.
4. 두 API 계산 결과에 각각 한 개 PostgreSQL 원장·FINISHED MLflow run,
   S3/API/MLflow의 동일 결과 bytes를 확인한다. GPU allocation count0과
   실제 CPU reservation seconds를 혼동하지 않고 backend·device class별로 기록한다.
5. 기존 terminal Job/원장/entity와 다른 서비스·Secret·PVC·쿼터를 보존한다.
   신규 계약·두 계산 실행의 정상 추가만 허용한다. 실패/취소/준비 비용도
   보존하고 시간이나 값이 없으면 null로 남긴다.
6. 자기 qualification Job/ConfigMap만 정리하고 최종 큐 반환과 worker Ready를
   확인한다. 실패한 native Job을 새 Job으로 대체하지 않는다.

기존 GPU workflow 취소·owner-loss 수용시험 근거는 유지한다. 이번 정상
CPU→GPU 실행만으로 전체 E7 실패 처리나 초기 HAIRP 네 pipeline의 완료를
대신하지 않는다. 실제 결과가 나오기 전에는 M1 완료로 표시하지 않는다.
