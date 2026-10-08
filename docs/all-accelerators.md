# 현재 가속기와 작업 템플릿

기존 플랫폼에서 Kubernetes GPU5대와 NPU5개의 검증된 템플릿 실행을 확인했다.
Intel NPU와 RTX5080은 같은 호스트에 있으므로 장비10개를 노드10대로 세지 않는다.
Jetson의 shared 슬롯 수도 물리 GPU 개수와 다르다.

## Console에서 실행하기

1. 작업 제출에서 아래 템플릿을 선택한다.
2. `lab-all-accelerators-v2` 정책으로 제출한다.
3. 네이티브 큐의 대기·할당·실행 상태와 결과를 확인한다.

공통 GPU와 Hailo 작업은 검증된 후보 중에서 자동 선택한다.
SDK가 다른 NPU는 각 모델에 맞는 템플릿을 사용한다.

| 장비 | 작업 / 템플릿 | 확인한 실행 범위 |
|---|---|---|
| RTX5060Ti, RTX5080, GB10, Orin Nano, AGX Orin | `cuda-sustained-auto-v2` / `gpu-fleet-auto-v2` | 각 GPU에서90초 CUDA integer 작업 |
| Hailo8 두 장치 | `hailo-resnet50-auto-20261008-v2` / `hailo-two-auto-20261008-v2` | 같은 HEF·100 input의 ResNet50 |
| Mobilint ARIES2 | `mobilint-candy-20261008-v1` | compiled Candy·고정 입력·반복 출력 일치 |
| Intel NPU3720 | `intel-conv-20261008-v1` | OpenVINO 직접 NPU CNN·NumPy 수치 기준 |
| Rockchip RK3399Pro | `rockchip-resnet18-host-20261008-v2` | 공식 ResNet18 단일 입력·연속 API2회 |
| Slurm Orin | `right-sizing-slurm-20261008-v1` | qualified CNN·추천·승인·결과 피드백 |

Kueue/Kubernetes/Slurm이 admission·quota·node binding을 담당한다.
플랫폼은 workload/runtime 검증과 결과·profile·MLflow·artifact·사용량 연결을 담당한다.
ArgoCD는 서비스 배포에 사용한다.

## 지원 범위

- CUDA integer 실행은 임의 AI 모델의 실행 가능성이나 성능 qualification이 아니다.
- Hailo는100 input에서 accuracy0.80/reference agreement0.97을 확인했다.
- Mobilint는 runtime 반복 검증이며 스타일 품질 평가가 아니다.
- Intel 생성 CNN의 수치 검증은 다른 모델의 정확도를 보장하지 않는다.
- Rockchip은 한 예제의 class812 일치다. Dataset accuracy나 장기간 안정성 검증이 아니다.
  Host-network bridge는 trusted lab 전용이며 tenant network isolation을 검증하지 않았다.
- Intel/Mobilint/Rockchip은 firmware identity가 불완전해 manual observe만 허용하고
  성능 추천 재사용은 `RUNTIME_FINGERPRINT_INCOMPLETE`로 보류한다.
- DEEPX는 endpoint/runtime/model 증거가 없어 BLOCKED다. AMD 실측, 다중 GPU 학습과
  임의 모델의 GPU↔NPU 자동 변환도 완료하지 않았다.

모델·입력·정밀도·품질 계약이 다른 결과를 GPU↔NPU 성능 순위로 합치지 않는다.
사용한 장비 수와 right-sizing 최적화 성공은 별개의 주장이다.

## 구현

[bounded runner](../src/resource_advisor/npu_probe.py),
[immutable contract 생성](../examples/build_npu_target.py),
[fingerprint 정책](../src/resource_advisor/policy.py),
[backend adapter](../src/resource_advisor/backends.py),
[NPU 배포 설정](../deploy/npu/README.md),
[추천 비교와 비용](right-sizing-comparison-results.md)을 연결한다.
