# 모빌인트 NPU와 RTX 5080: Candy 실행 구성의 예비 비교

2026-10-12 실제 장비에서 측정 경계를 수정한 protocol4로 3회씩 측정했다.
워밍업 이후 평균 호출 시간은 RTX 5080 **2.816ms**, 모빌인트 NPU **26.020ms**였다.
**NPU MXQ의 원본 모델과 공개 Candy ONNX의 가중치 동일성은 확인되지 않았다.**
따라서 이 수치는 두 실행 구성의 관측값이며, 동일 모델의 하드웨어 성능 우위나
개선율을 입증하는 결과가 아니다.

| 측정 항목 | 모빌인트 NPU | RTX 5080 |
|---|---:|---:|
| 워밍업 이후 평균 호출 시간 | 26.020ms | 2.816ms |
| 워밍업 이후 empirical p95 | 26.429ms | 2.858ms |
| 측정 루프의 호출 처리량 | 38.43회/초 | 355.15회/초 |
| 모델/런타임 설정 구간 평균 | 76.203ms | 163.047ms |
| 새 프로세스의 첫 워밍업 호출 평균 | 29.147ms | 7,803.136ms |
| 실험 Job 평균 JCT | 2.000초 | 10.333초 |
| 본 측정의 장비 할당 시간 합계 | 6 device-seconds | 28 device-seconds |

GPU의 첫 워밍업 호출은 블록별 1.696초, 1.695초, 20.018초였다. NPU는
30.465ms, 28.135ms, 28.842ms였다. 각 세션은 새 프로세스지만 장비/노드의 캐시는
초기화하지 않았다. GPU 첫 호출의 편차를 JIT, 알고리즘 탐색, 캐시 등으로 분리하는
실험은 수행하지 않았다. 첫 워밍업은 정상 상태의 호출 시간에서 제외했지만 Job JCT에는
포함된다. GPU Job JCT도 5초, 4초, 22초로 달랐다.

Job JCT는 아래 검증 작업까지 포함하는 실험 하네스의 완료 시간이다. GPU에는 측정 후
별도 세션의 실행 배치 검사가 추가돼 있으므로 일반 서비스의 end-to-end 지연으로
해석해서는 안 된다. 정상 상태의 호출 처리량도 입력 준비, 모델 설정, 첫 워밍업,
대기 시간을 제외한 값이다.

## 조건과 측정 경계

- 장비별 새 Kubernetes Job/프로세스 3개. 각 Job에서 워밍업 5회 후 30회 측정했다.
  protocol4의 본 측정은 총 6개 Job, 180개 호출이다. 장비별 90개 호출은 3개 Job
  안에 묶인 반복이며 90개 독립 실험으로 취급하지 않는다. p95는 90개 호출을 합친
  선형 보간 표본 분위수이며 모집단 p95의 추정 정확도나 통계적 유의성을 주장하지 않는다.
- 시간 블록마다 두 장비를 한 번씩 실행했다. 순서 seed는 20261012이며
  `NPU→GPU`, `NPU→GPU`, `GPU→NPU` 순서를 결과 수집 전에 정했다.
- 배치 1, 224×344×3 float32 합성 입력, NumPy seed 20261008,
  uniform[-1,1]. 두 런타임에 같은 float 값의 입력을 전달했다.
  입력 SHA256은 `005d18ceb13076219f5a4cead7611755c19859334d0671e7545cbded1bad6edc`다.
- 타이머 경계는 준비된 host 입력부터 C-contiguous HWC host 출력 복사 완료까지의
  blocking 호출이다. GPU의 HWC→NCHW 입력 배치는 타이머 전에 준비했다.
  GPU 출력의 NCHW→HWC 변환과 C-order 복사, NPU 출력의 C-order 복사는 타이머에 포함된다.
  호출 후 연속 배열 여부·해시·finite 검사 비용은 측정 시간에 포함하지 않는다.
- 두 장비 모두 CPU request/limit 1, 메모리 request 1GiB/limit 4GiB, 물리 장비
  resource 1을 사용했다. 기존 queue quota 및 driver/firmware는 변경하지 않았다.
- 모빌인트는 기존 `style_candy.mxq`, qbruntime 1.4.0, NumPy 1.26.4,
  Cluster0/Core0 한 코어를 사용했다. 모델 SHA256은
  `aa158a35735a876cc9f9220f9967b711b8e49ce5de49126f0597e994c6b543da`다.
  float32 입출력은 확인했지만 MXQ 내부 연산 정밀도는 이번에 확인하지 않았다.
- GPU는 실제 `NVIDIA GeForce RTX 5080`을 CUDA Driver API로 확인했다.
  ONNX Runtime GPU 1.23.2, CUDA 12.8 라이브러리, cuDNN 9.10.2.21,
  float32 및 TF32 비활성화를 사용했다. CUDA Driver API가 보고한 지원 버전은
  13020이다. 이는 NVIDIA 커널 드라이버 패키지 버전 번호가 아니다.
- 공개 ONNX Candy-9의 입력·출력 shape 메타데이터를 224×344로 바꾸고, 고정
  initializer 입력을 제거한 뒤 IR4 및 ORT basic constant folding으로 준비했다.
  원본 initializer 값은 모두 byte 단위로 동일함을 확인했다. 이 확인은
  **공개 ONNX 원본과 준비된 GPU 모델 사이**의 확인이며 MXQ와의 확인이 아니다.
  준비된 GPU 모델 SHA256은
  `ab607dd30842a4b96b7fbbf7ddf01a18509a308e0584bcfcfc6c6198f3c2ef40`다.

공개 Candy-9의 공식 입력 범위는 RGB [0,255]다. 이번에는 기존 NPU 실험의 합성
입력 [-1,1]을 그대로 재사용했다. 따라서 실제 이미지의 품질이나 올바른 스타일 변환을
검증한 실험이 아니다. 출력 범위도 NPU 약 [-0.488,1.449], GPU 약 [-138.603,363.064]로
달랐으며, 전처리·출력 정규화의 동등성 역시 확인되지 않았다.
또한 NPU 한 코어와 GPU 한 장을 사용했으므로 NPU 보드 전체 처리량 비교가 아니다.
[공개 Candy 원본·입력 조건](https://github.com/onnx/models/blob/c5cae0f1942c8d7727372c7b1f6b485aa39e4421/validated/vision/style_transfer/fast_neural_style/README.md),
[CUDA 실행 제공자 설정](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider).

## 수정 전 pilot과 보존 범위

처음 protocol3에서 GPU 평균 2.688ms, NPU 평균 24.790ms가 기록됐다.
검토 중 GPU의 NCHW→HWC transpose 뒤 `np.array(..., copy=True)`가 기본 K order를
사용해 planar stride를 유지한다는 것을 확인했다. 모양은 HWC였지만 연속 HWC 배열이
아니었고, 타이머 이후 `tobytes()`가 수행한 연속 직렬화 비용이 빠져 있었다.
따라서 당시 기록의 contiguous 경계 표기는 실제 호출과 일치하지 않았다.

이전 결과를 수정하거나 삭제하지 않았다. `probe-v2.py`, `protocol-v3.json`,
`capture-v3.json`, `samples-v3.csv`, `summary-v3.json`과 native 기록을 pilot으로 보존했다.
과거 파일의 경계 표기는 당시 실행 증거이며 정확한 설명으로 승인한 것이 아니다.
protocol4는 두 런타임 모두 명시적 `order="C"` 복사를 타이머 안에 넣고 매 호출 뒤
C-contiguity를 확인한다. 같은 이미지·자원·배정 순서로 양쪽 3개 Job을 다시 측정했으며,
이 문서의 최종 표에는 protocol4 결과만 사용했다. 두 실행 사이 수치 차이를 출력
복사의 인과적 효과로 해석하지 않는다.

## 기록과 확인 결과

protocol4 본 Job 6개는 모두 성공했고, 각 Job의 30개 출력은 finite/matching shape,
C-contiguity 및 동일 런타임 안의 element agreement 1.0을 만족했다. 이는 반복 실행
일관성 확인이며 모델 정확도나 두 모델의 동등성 검증이 아니다.

GPU에서 CPU fallback을 비활성화했다. 별도 감사 세션에서 실행한 64개 모델 operator는
모두 CUDAExecutionProvider에 배치됐고, trace를 보존했다. 등록 provider 목록에는
CPUExecutionProvider도 나타나지만 등록 목록 자체는 실제 실행 배치 증거가 아니다.
[등록 provider와 실행의 구분](https://onnxruntime.ai/docs/api/python/api_summary.html).

가속기 Job은 protocol4 본 측정 6개, 이전 pilot 6개, 준비 9개로 총 21개이며
18개 성공, 3개 실패다. GPU 준비 실패는 NVRTC 누락, 과도한 provider 목록 검사,
1GiB 제한의 OOMKilled였다. NVRTC를 격리 이미지에 추가하고 등록/실행을 구분했으며,
양쪽 메모리 limit을 4GiB로 맞춘 뒤 출력 layout 문제를 수정했다. 이전 코드와
protocol revision도 남겼다. 추가 CPU 원본 탐색 Job은 3개 중 1개 성공, 2개 실패였다.
첫 탐색 Pod는 더 이상 조회되지 않아 Job의 DeadlineExceeded와 당시 FailedMount
event를 보존했다. 전체 시도 24개 모두 종료됐다.

PodScheduled부터 컨테이너 종료까지의 예약 비용은 GPU 준비 62초+pilot 66초+
최종 측정 28초=156 device-seconds, NPU 준비 6초+pilot 5초+최종 측정 6초=
17 device-seconds다. 각 GPU/NPU resource 1 기준의 device-seconds이며 소비 전력·
GPU utilization·NPU 코어 utilization 측정값이 아니다. 원본 탐색 및 로컬 패키지/이미지
준비 비용은 별도이고 이 합계에 포함되지 않는다. Kubernetes timestamp는 초 단위이므로
이 비용과 Job JCT도 초 단위다. 활성 실험 작업은 남아 있지 않다.

[원시 호출 CSV](evidence/mobilint-5080-20261012-v1/samples.csv),
[요약 JSON](evidence/mobilint-5080-20261012-v1/summary.json),
[모든 시도](evidence/mobilint-5080-20261012-v1/attempts.csv),
[native 기록 아카이브](evidence/mobilint-5080-20261012-v1/native-records-public.tar.gz),
[재계산 코드](evidence/mobilint-5080-20261012-v1/recompute.py)를 보존했다.
공개 기록의 node·registry·IP는 치환했고 원본/공개 SHA256 대응표를 남겼다.
원본 기록은 private 실험 디렉터리에 있으며 공급사 binary/SDK는 Git에 넣지 않았다.
모델 준비 스크립트 재실행의 SHA256 일치와 최종·pilot CSV/summary 재계산 일치를 확인했다.
실제 immutable ConfigMap의 코드가 protocol의 SHA256과 같은지도 확인했다.
Resource Advisor production 스케줄링 경로에는 이 비교 결과를 반영하지 않았다.

## NAIS 자기소개서 활용 범위

이번 결과로 “NPU 대비 9배 개선” 같은 성능 개선율을 주장할 수 없다.
동일 원본 모델, 정밀도/전처리/출력 조건 및 보드 사용 범위를 맞춘 후 다시 비교해야 한다.
이번 작업은 다음과 같이 사실에 맞게 설명할 수 있다.

> 이종 가속기의 추론 호출 시간과 최초 실행 준비 비용을 분리하고, 측정 경계의
> 불일치를 수정한 뒤 장비별 3회 세션·총 180개 호출로 다시 측정했습니다.
> 원시 기록과 이전 시도를 보존하고 모델 동등성의 한계를 확인해, 검증되지 않은
> 하드웨어 개선율 주장과 운영 정책 반영을 보류했습니다.

## RTX 5060 Ti 추가 측정

2026-10-12 같은 GPU artifact·입력·수정된 probe로 RTX 5060 Ti를 3회 추가 측정했다. 워밍업 후 평균7.821ms, empirical p95 9.052ms였다. 이전 장비와 시간 블록을 함께 무작위 배정한 실험은 아니다. [추가 측정의 한국어 설명과 원시 기록](candy-5060ti-20261012-v1-results.ko.md)을 참조한다.
