# 03. 하드웨어 인벤토리와 fingerprint 계약

관측: 2026-10-29 02:29:16Z–02:32:15Z 읽기 전용([CURRENT-STATE-AUDIT.md §4](CURRENT-STATE-AUDIT.md#4-현재-장비-상태-오늘-읽기-전용-관측)). 이 단계에서 새 장비 관측·실행은 하지 않았다. 노드 이름은 alias다.

## 1. 오늘 상태 요약 (실험 계획 입력)

| alias | 장치 | resource 단위 | 상태 | 추천 후보 여부 |
|---|---|---|---|---|
| S1 | RTX5060Ti | `nvidia.com/gpu` 물리 1 | BLOCKED (다른 project 점유) | 아니오 |
| S2 | RTX5080 | `nvidia.com/gpu` 물리 1 | BLOCKED | 아니오 |
| S2 | Intel NPU | `npu.intel.com/accel` 물리 1 | AVAILABLE(resource), runtime·firmware 결합 UNVERIFIED | 검증 전 아니오 |
| S3 | GB10 | `nvidia.com/gpu` 물리 1 | BLOCKED | 아니오 |
| S4 | Mobilint ARIES2 | `mobilint.com/npu` 물리 1 | AVAILABLE(resource), runtime manifest 결합 UNVERIFIED | 검증 전 아니오 |
| E-Nano-A | Orin Nano | `nvidia.com/gpu.shared` **logical slot** 2 | AVAILABLE(resource), 학습 variant 없음 | 추론만, 검증 후 |
| E-Nano-B | Orin-class | logical slot 2 | Kueue flavor 없음(route BLOCKED) | 아니오 |
| E-Hailo-A / B | Hailo8 | `hailo.ai/h8` 물리 1 | A BLOCKED / B AVAILABLE(resource) | B: HEF 실행 검증 후 |
| E-AGX, E-RK | AGX Orin, RK3399Pro | — | UNREACHABLE | 아니오 |
| K8s CPU 노드 | CPU | cpu/memory | AVAILABLE | CPU 경로 재검증 후 |
| Slurm VM | CPU (QEMU TCG) | cpu 2, 3072 MiB | AVAILABLE | CPU host-copy 경로 재검증 후 |
| physical Slurm | Orin·Pi·controller | — | UNREACHABLE | 아니오 |
| AMD/ROCm, DEEPX, HAMi | — | 광고 resource 없음 / plugin 없음 | UNSUPPORTED/BLOCKED | 아니오 |

"AVAILABLE(resource)"는 Kubernetes가 resource를 노출한다는 뜻이며 runtime·모델 실행 성공이 아니다.

## 2. Fingerprint 계약 (`HardwareFingerprint`, `JctProfile`)

| 항목 | 규칙 |
|---|---|
| vendor/model/device class | capability와 정확히 같아야 profile 재사용 (`PROFILE_HARDWARE_MISMATCH`) |
| memory | `memory_model`이 `unified`(Jetson)·`host`이면 `dedicated_memory_mib`를 가질 수 없다. 공유 메모리 ≠ 전용 VRAM |
| allocation | `virtual_slot`(gpu.shared, HAMi vGPU) ⇔ `logical_slot=True`. slot 수 ≠ 물리 장치 수. `physical_device_ref` 미상이면 null |
| driver/SDK/firmware | `runtime_versions`가 capability와 같아야 함 (`PROFILE_RUNTIME_MISMATCH`). 기존 규칙대로 Intel/Mobilint/Rockchip NPU는 `firmware_identity`가 없으면 `RUNTIME_FINGERPRINT_INCOMPLETE`로 제외되며, 추천 경로에서는 operational 완화로 이를 무시하지 않는다 |
| workload | precision·batch·input shape·dataset version + workload/context signature 일치 (`PROFILE_WORKLOAD_MISMATCH`) |
| 시간 | `measured_at`(미래 거절), `valid_for_seconds`와 정책 `max_profile_age_seconds` 중 작은 값 (`PROFILE_STALE`) |
| 품질·출처 | `quality_passed`, `synthetic=False`, `repeats`, `job_ids`, `artifact_refs`, `source` |
| 실제 노드 | profile의 `node_ref`는 node 모드에서 capability 노드, pool 모드에서 member 중 하나 |

## 3. 수집 도구 (읽기 전용)

```bash
rtk proxy uv run --locked python examples/hetero_collect.py inventory --kubectl --local > inventory.json
rtk proxy uv run --locked python examples/hetero_collect.py inventory --nodes-json nodes.json --sinfo-text sinfo.txt
rtk proxy uv run --locked python examples/hetero_collect.py probes
```

- `kubectl get nodes`의 allocatable extended resource를 device class·allocation 단위와 함께 기록한다. GPU product label은 `label_hints`로만 남기며 검증값이 아니다. `runtime_verified=false`, `driver_sdk_firmware=null`이 기본이다.
- `sinfo -N -h -o '%N|%G|%f|%c|%m|%T'` 출력의 GRES·feature는 설정값이다.
- `--local`은 설치된 `nvidia-smi`·`hailortcli`·`rocm-smi`만 읽고, 없으면 `TOOL_NOT_FOUND`.
- 실제 CPU/GPU/NPU 실행 확인은 기존 probe(`cuda_probe` = CUDA smoke, `npu_probe` = NPU 추론 smoke, `cpu_benchmark`)를 native Job으로 실행할 때만 생긴다. smoke ≠ AI 학습, GPU 학습 ≠ NPU 추론.
