# Right-sizing hardware evidence matrix

장치 등록, runtime 동작, 모델 검증과 현재 접근 가능성을 분리한다. 이 표는
2026-10-07 실험, 10-08 Slurm 복귀와 별도 전체 가속기 실행 증거의 범위다.
자동 capability 승격이나 전체 right-sizing 완료가 아니다.

| 장치/runtime | 등록/접근 | 실행 검증 | 이번 새 증거 | 추천에 허용되는 범위 |
|---|---|---|---|---|
| RTX5080 / PyTorch2.8.0+cu128, CUDA12.8, driver595.84 | 실제 접근 | MODEL_VERIFIED | GPU F0 4개, 신규 GPU API97개 + reference45개 | W1 matmul/W2 CNN의 고정 logical identity, 6 CPU/memory 구성, physical GPU1 |
| Hailo8 두 장치 / HailoRT·driver·firmware4.23.0 | 실제 접근, 각각 NPU1 | MODEL_VERIFIED | 기존 단일 장치 증거 보존 + 새 두 장치 API 실행 | 같은 HEF/100 input/80% accuracy/97% agreement 계약 |
| Orin GPU / 기존 Slurm qualified PyTorch runtime | 10-08 controller/native RPC 복구 | 새 고정 CNN MODEL_VERIFIED | 새 native Job5 / observe3 / 승인 feedback / next source4 | 단일 고정 후보 lifecycle; Slurm search/backend 속도 비교 아님 |
| 다른 NVIDIA/Jetson node | GPU5 native API 실행 | workload별 별도 확인 필요 | 같은90초 CUDA integer 작업을 각 GPU에서 성공 | smoke를 W1/W2/임의 모델 performance qualification으로 재사용하지 않음 |
| Mobilint ARIES2 / qbruntime1.4.0 | NPU1, 실제 접근 | bounded Candy runtime 반복 검증 | 새 API 성공 + fresh F0 | 수동 observe; 스타일 품질·firmware 미검증, performance reuse 보류 |
| Intel NPU3720 / OpenVINO2026.3.1 | NPU1, 실제 접근 | generated CNN numerical verification | 직접 NPU API 성공 + fresh F0, CPU fallback 금지 | 수동 observe; 임의 모델 품질·firmware 미검증, performance reuse 보류 |
| Rockchip RK3399Pro / RKNNLite1.7.1 | NPU1, 실제 접근 | 공식 ResNet18 단일 입력 검증 | old transport timeout 보존 + host bridge 연속 API 성공2 | trusted lab 수동 observe; SDK/server mismatch·firmware·isolation 미완료 |
| Pi DEEPX | 기존 PCIe 조사에서 endpoint 미탐지 | BLOCKED | 새 runtime/model 성공 없음 | supported·실측 완료 주장 금지 |
| AMD | 접근 가능한 실제 장비 없음 | PLANNED/interface-only | 없음 | capability schema/adapter 계약만, 성능 실측 없음 |

[GPU 원시 qualification/result](../evidence/right-sizing-gpu-v1.json),
[reference 원시 결과](../evidence/right-sizing-reference-recovery-v2.json),
[Hailo 원시 qualification/quality](../evidence/right-sizing-hailo-v1.json),
[기존 Slurm 모델 실행](slurm-jetson-runtime-results.md),
[Slurm response recovery](slurm-runtime.md)를 근거로 한다.
추가 NPU의 native UID/result/source snapshot과 비용·한계는
[전체 가속기 실행](../all-accelerators.md)에 별도 보존한다.

GPU 생성 CNN과 Hailo ResNet50은 모델·정밀도·입력·quality·timing boundary가
다르므로 **NOT_COMPARABLE**다. 동일 logical task의 CUDA equivalent를 모델/입력/
quality 계약까지 별도로 qualify하기 전 GPU↔NPU 성능 순위를 만들지 않는다.
GPU count=1이며 DDP, 학습 scaling, checkpoint/restart를 검증한 다중 GPU
right-sizing 성과로 표현하지 않는다. Hailo 내부 utilization/memory/power는
unknown이다. 요청량/예약초와 실제 utilization도 같은 지표가 아니다.
