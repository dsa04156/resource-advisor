# RTX 5060 Ti 추가 측정: 모빌인트 NPU·RTX 5080 기록과 비교

연결된 실제 장비는 **NVIDIA GeForce RTX 5060 Ti**다. 일반 RTX 5060을 측정한 결과가 아니다.
2026-10-12 같은 Candy GPU 모델·입력·코드로 새 Job 3개를 실행했고, 워밍업 이후
평균 호출 시간은 **7.821ms**, empirical p95는 **9.052ms**였다.

| 측정 항목 | 모빌인트 NPU: 이전 측정 | RTX 5080: 이전 측정 | RTX 5060 Ti: 추가 측정 |
|---|---:|---:|---:|
| 워밍업 후 평균 호출 시간 | 26.020ms | 2.816ms | 7.821ms |
| 워밍업 후 empirical p95 | 26.429ms | 2.858ms | 9.052ms |
| 측정 루프 호출 처리량 | 38.43회/초 | 355.15회/초 | 127.87회/초 |
| 모델/런타임 설정 구간 평균 | 76.203ms | 163.047ms | 466.772ms |
| 새 프로세스 첫 호출 평균 | 29.147ms | 7,803.136ms | 19,126.989ms |
| 실험 Job 평균 JCT | 2.000초 | 10.333초 | 22.333초 |

이 기록에서 정상 상태의 호출 시간은 5080, 5060 Ti, NPU 순서였다. GPU 두 장비에는
**같은 준비된 ONNX artifact와 숫자 입력**을 사용했다. 다만 5060 Ti는 나중에 추가한
단일 장비 코호트이며 이전 5080/NPU와 동시에 무작위 배정한 실험이 아니다. 시간·
노드·캐시 조건의 효과를 분리하지 않았고 일반적인 하드웨어 성능 개선율이나
통계적 유의성을 주장하지 않는다.

NPU는 기존 Cluster0/Core0 한 코어이고, MXQ와 ONNX의 원본 가중치·내부 정밀도·
전처리/출력 정규화의 동일성이 여전히 확인되지 않았다. 입력은 기존 합성 uniform[-1,1]
fixture라 공개 Candy의 RGB[0,255] 실제 이미지 조건을 검증한 것이 아니다. NPU 보드
전체 성능이나 두 모델의 정확도·에너지 효율을 비교한 결과로 해석할 수 없다.
[앞선 실험의 조건과 모델 한계](mobilint-5080-20261012-v1-results.ko.md).

## 동일하게 유지한 조건

- 배치1, HWC224×344×3 float32, NumPy seed20261008. 입력 SHA256은
  `005d18ceb13076219f5a4cead7611755c19859334d0671e7545cbded1bad6edc`다.
- GPU 모델 SHA256은
  `ab607dd30842a4b96b7fbbf7ddf01a18509a308e0584bcfcfc6c6198f3c2ef40`이며
  앞선 5080의 준비된 모델과 같다. ORT GPU1.23.2, NumPy1.26.4,
  FP32/TF32 비활성화 및 같은 image digest를 사용했다.
- protocol4의 수정된 probe를 byte 단위로 그대로 재사용했다. 준비된 host 입력부터
  C-contiguous HWC host 출력 복사 완료까지 blocking 호출을 측정했다. GPU 입력 layout은
  타이머 전에 준비하고 출력 layout 변환/C-order 복사는 타이머 안에 수행한다.
- CPU request/limit1, 메모리 request1Gi/limit4Gi, GPU resource1을 사용했다.
  새 Job/프로세스3개에서 각 warmup5회 후30회, 총90호출을 측정했다. 같은 물리
  5060 Ti의 3개 세션이며 90개의 독립된 하드웨어 실험이 아니다. p95는 이 90개 호출의
  선형 보간 표본 분위수다. 장비/노드 캐시를 초기화하지 않았다.
- CUDA Driver API로 각 Job의 `NVIDIA GeForce RTX 5060 Ti`를 확인했다.
  지원 API 버전은13020이며 커널 driver 패키지 버전 번호는 아니다.

5060 Ti의 첫 워밍업 호출은 블록별 **27.347초, 27.178초, 2.856초**였다.
원인을 JIT·알고리즘 탐색·캐시 등으로 분리하지 않았다. 첫 호출 비용은 정상 상태
평균과 루프 처리량에서 제외되고 실험 Job JCT에는 포함된다. GPU는 측정 뒤 별도
세션의 CUDA 배치 검사를 수행하므로 이 Job JCT는 일반 서비스 완료 시간과 다르다.
배치 검사 세션은 측정 루프 밖이며 Job 안에 있다.

## 보존과 확인

새 준비 Job1개와 본 Job3개가 모두 성공했다. 각 출력은 finite/matching shape,
C-contiguity 및 세션 내 element agreement1.0을 만족했다. CPU fallback을 비활성화했고
각 감사 trace의64개 모델 operator 모두 CUDAExecutionProvider 실행임을 확인했다.
등록된 provider 목록만으로 GPU 실행을 판단하지 않았다.

새 GPU 예약 비용은 PodScheduled부터 컨테이너 종료까지 준비 **35초**+
본 측정 **64초**=**99 device-seconds**였다. 준비 비용에는 컨테이너 시작 전 대기도
포함된다. 이는 예약 시간이며 소비 전력이나 GPU utilization 측정값이 아니다.
Kubernetes timestamp는 초 단위이고 로컬 패키지/이미지 준비 비용은 포함하지 않는다.
이번 추가 Job4개는 모두 종료됐다.

[원시90호출 CSV](evidence/candy-5060ti-20261012-v1/samples.csv),
[요약 JSON](evidence/candy-5060ti-20261012-v1/summary.json),
[모든 추가 시도](evidence/candy-5060ti-20261012-v1/attempts.csv),
[native 기록](evidence/candy-5060ti-20261012-v1/native-records-public.tar.gz),
[재계산 코드](evidence/candy-5060ti-20261012-v1/recompute.py)를 보존했다.
이전 실험의 데이터 파일은 덮어쓰지 않았다. 원본/공개 SHA256 대응표를 남기고 private
node/registry/IP를 치환했다. 공급사 binary/SDK는 Git에 넣지 않았다.
모델·입력·코드·이미지·native 결과와 이전 참조 summary의 SHA256 및 CSV/summary
재계산 일치를 확인했다. Resource Advisor production 정책에는 반영하지 않았다.

자기소개서에는 “동일 GPU 모델 artifact와 입력으로 RTX 5060 Ti에서 3회 세션·90개 호출을
추가 측정하고, 이전 GPU/NPU 관측값과 측정 조건을 함께 기록했다”는 사실을 활용할 수 있다.
이 결과로 NPU 대비 하드웨어 개선율을 입증했다고 쓰지는 않는다.
