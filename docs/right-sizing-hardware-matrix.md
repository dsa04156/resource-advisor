# Right-sizing hardware evidence matrix

장치 등록, runtime 동작, 모델 검증과 현재 접근 가능성을 분리한다. 이 표는
2026-10-07 실험 및 10-08 Slurm 복귀 증거의 범위이며 자동 capability 승격이 아니다.

| 장치/runtime | 등록/접근 | 실행 검증 | 이번 새 증거 | 추천에 허용되는 범위 |
|---|---|---|---|---|
| RTX5080 / PyTorch2.8.0+cu128, CUDA12.8, driver595.84 | 실제 접근 | MODEL_VERIFIED | GPU F0 4개, 신규 GPU API97개 + reference45개 | W1 matmul/W2 CNN의 고정 logical identity, 6 CPU/memory 구성, physical GPU1 |
| Hailo8 / HailoRT·driver·firmware4.23.0 | 실제 접근 | MODEL_VERIFIED | ResNet50 F0 1개, 원본 observe3 + approved fixed1 | 같은 HEF/100 input/80% accuracy/97% agreement 계약의 단일 구성 |
| Orin GPU / 기존 Slurm qualified PyTorch runtime | 10-08 controller/native RPC 복구 | 새 고정 CNN MODEL_VERIFIED | 새 native Job5 / observe3 / 승인 feedback / next source4 | 단일 고정 후보 lifecycle; Slurm search/backend 속도 비교 아님 |
| 다른 NVIDIA/Jetson node | 기존 inventory/CUDA smoke | workload별 별도 확인 필요 | 새 W1/W2 model qualification 없음 | 등록 또는 smoke만으로 신규 model performance profile을 재사용하지 않음 |
| Mobilint/Rockchip 등 NPU | 기존 inventory/일부 telemetry | 신규 logical model evidence 없음 | 없음 | 해당 SDK/모델/quality qualification 전 resource-selection 후보 승격 금지 |
| Pi DEEPX | 기존 PCIe 조사에서 endpoint 미탐지 | BLOCKED | 새 runtime/model 성공 없음 | supported·실측 완료 주장 금지 |
| AMD | 접근 가능한 실제 장비 없음 | PLANNED/interface-only | 없음 | capability schema/adapter 계약만, 성능 실측 없음 |

[GPU 원시 qualification/result](evidence/right-sizing-gpu-v1.json),
[reference 원시 결과](evidence/right-sizing-reference-recovery-v2.json),
[Hailo 원시 qualification/quality](evidence/right-sizing-hailo-v1.json),
[기존 Slurm 모델 실행](slurm-torch-results.md),
[Slurm response recovery](slurm-response-recovery.md)를 근거로 한다.

GPU 생성 CNN과 Hailo ResNet50은 모델·정밀도·입력·quality·timing boundary가
다르므로 **NOT_COMPARABLE**다. 동일 logical task의 CUDA equivalent를 모델/입력/
quality 계약까지 별도로 qualify하기 전 GPU↔NPU 성능 순위를 만들지 않는다.
GPU count=1이며 DDP, 학습 scaling, checkpoint/restart를 검증한 다중 GPU
right-sizing 성과로 표현하지 않는다. Hailo 내부 utilization/memory/power는
unknown이다. 요청량/예약초와 실제 utilization도 같은 지표가 아니다.
