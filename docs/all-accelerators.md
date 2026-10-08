# 전체 가속기 연결 결과 — 2026-10-08

기존 플랫폼을 확장해 Kubernetes GPU 5대와 NPU 5개에서 API 작업 실행을 확인했다.
Slurm Orin의 기존 실행 경로도 유지한다. GPU 5대와 NPU 5개는 노드 10대라는 뜻이
아니다. Intel NPU와 RTX5080은 같은 호스트에 있고 Jetson의 shared 슬롯은 물리 GPU
개수와 다르다. 이 기록은 장비 사용 경로의 검증이며 right-sizing 전체 연구 완료가 아니다.

## 웹에서 사용하는 방법

기존 Console의 작업 제출 화면에서 아래 템플릿을 선택한다. GPU를 먼저 고르지
않고 `lab-all-accelerators-v2` 정책으로 제출한다. GPU와 Hailo의 여러 후보는
검증된 workload 후보·현재 요청 headroom·최근 할당을 평가해 선택한다.
SDK가 다른 NPU는 각각 검증한 모델 템플릿을 선택해야 한다.

| 장비 | 작업/템플릿 | 실제 실행 범위 |
|---|---|---|
| RTX5060Ti, RTX5080, GB10, Orin Nano, AGX Orin | `cuda-sustained-auto-v2` / `gpu-fleet-auto-v2` | 90초 CUDA integer 작업; GPU별 다른 API 요청 5개 성공 |
| Hailo8 두 장치 | `hailo-resnet50-auto-20261008-v2` / `hailo-two-auto-20261008-v2` | 같은 HEF·100 input 계약, 각 장치 성공 |
| Mobilint ARIES2 | `mobilint-candy-20261008-v1` | 기존 compiled Candy, 고정 입력과 10회 출력 반복 일치 |
| Intel NPU3720 | `intel-conv-20261008-v1` | OpenVINO 직접 NPU 생성 CNN, NumPy 독립 수치 기준 통과 |
| Rockchip RK3399Pro | `rockchip-resnet18-host-20261008-v2` | 공식 ResNet18의 단일 입력, 10회 class812; trusted lab 전용 |
| Slurm Orin | `right-sizing-slurm-20261008-v1` | 기존 qualified 고정 CNN 및 승인·결과 피드백 경로 |

접수 → native queue → admission → 실행 → 결과·profile·accounting의 기존 경로를
사용한다. MLflow run과 result artifact의 연결도 확인했다. Native admission,
quota, node binding은 Kueue/Kubernetes/Slurm 기능이다. ArgoCD는 platform service
배포에 사용하며 runtime Job 제출에는 사용하지 않는다.

## 실제 증거

[원시 API 기록](evidence/all-accelerators-20261008-v1.json),
[native ownership 보충 증거](evidence/all-accelerators-20261008-native-proof-v2.json),
[재계산 결과](evidence/all-accelerators-20261008-v1-summary.json)를 연결한다.
API cohort는 17건 중 성공12·실패5다. 성공한 12건은 profile, MLflow run,
artifact/tracking digest와 terminal usage를 가진다. source parent commit과 실제
실행 source digest를 구분하며 당시 runner 원본은
[snapshot 디렉터리](evidence/npu-runner-snapshots)에 보존했다.

Rockchip의 이전 pod-owned transport는 재실행 hang/timeout을 보였다. host proxy2.1을
단일 서비스로 유지하고, 운영자가 variant/environment digest를 allowlist한 전용
host-network backend를 추가했다. 서비스 전환 뒤 첫 API 요청도 timeout됐으며
이를 삭제하지 않았다. idle 장치 reset·plugin 재등록 뒤 아래 두 요청은 **사이에
reset/proxy restart 없이** 성공했다.

- `j-a79b2434c9fe485eaf42a8f4821deb18` / native UID `cd3bd186-5f26-423b-acdf-44eb9114f050`
- `j-82bea2f57763449c86b3fdcac342b3fc` / native UID `f00d0138-ec02-408d-8d6e-ce8e5efc6760`

별도 fresh Pod 2건도 성공했다. 이는 제한된 재실행 증거다. USB/session hang의
근본 원인을 확정하거나 재부팅·장기간 안정성·tenant network isolation을
검증한 것은 아니다. SDK1.6.0 matched-version 시험은 ARM64에서 NTB를 요구해
현재 USB 장비에서 실패했다. SDK/driver/firmware를 임의 업그레이드하지 않았다.
이 local host bridge는 공급사의 공식 Docker 권장 구성이 아니다.

## 품질과 추천 경계

Hailo는 100 input에서 accuracy0.80/reference agreement0.97이다. Mobilint의
반복 일치는 스타일 품질 평가가 아니고, Intel 생성 CNN의 수치 기준은 임의 모델
정확도가 아니며, Rockchip의 한 예제 class 일치는 dataset accuracy가 아니다.
서로 다른 모델·입력·정밀도·quality contract의 시간을 GPU↔NPU 속도 순위로
합치지 않는다. CUDA integer 성공도 임의 AI 모델 실행 qualification을 대신하지 않는다.

Intel/Mobilint/Rockchip은 host firmware identity를 충분히 관측하지 못했다.
명시적인 lab `observe` 실행은 허용하되 `RUNTIME_FINGERPRINT_INCOMPLETE`로
performance recommendation/profile reuse를 보류한다. 오래된 v1 NPU capability도
이 가드를 통과하지 못한다. Rockchip host proxy digest는 운영자 attestation이고
runner 안에서 host binary를 직접 해시한 값이 아니다. isolation_verified는 false다.
NPU 내부 memory/power/utilization은 관측하지 않은 값이며 host RSS와 구분한다.

DEEPX는 실제 endpoint/runtime/model evidence가 없어 **BLOCKED**다. Slurm Pi가
CPU node로 동작하는 사실을 NPU 지원으로 표현하지 않는다. AMD, 다중 GPU 학습,
NPU 학습과 arbitrary model GPU↔NPU 자동 변환도 이번 범위에 포함되지 않는다.

## 비용과 재계산

확인된 API 예약량은 GPU 요청 단위459초, NPU557초, CPU1016 core초다. GPU 요청 단위는
`physical_device`273초와 `virtual_slot`186초를 구분해야 한다. 요약의 `gpu_seconds`459는
이 요청 단위 합이며 물리 GPU459초나 utilization 적분값이 아니다. 이 중 Rockchip
실패4건의 알려진 NPU 예약량은 514초이며 compute time은 unknown이다.
남은 실패1건은 미시작 관측과 비용 null을 보존한다. qualification/진단/제어
Pod의 전체 비용도 아직 완전한 ledger가 없다. 따라서 위 수치는 **알려진 API
예약량의 합**이며 전체 실험 비용이나 zero-cost failure 주장으로 사용할 수 없다.
예약량은 실제 utilization이 아니다. profiling 비용 회수·성능 향상·ROI를 주장하지 않는다.
이전 worker가 제출한 GPU4건은 원본 Pod termination receipt가 없어 retained native
Job UID/Complete와 captured ledger로 연결했다. 그 4건의 원본 Pod 시간을 별도로
재검증했다고 주장하지 않는다. 나머지 captured receipt 경로는 종료 phase/exitCode와
scheduled/container 종료 시각을 usage에 대조한다.

기본 auditor는 missing reservation을 거절한다. 불완전한 비용을 그대로 보고하려면:

```bash
rtk proxy .venv/bin/python examples/audit_accelerator_enablement.py \
  docs/evidence/all-accelerators-20261008-v1.json \
  --receipts docs/evidence/all-accelerators-20261008-native-proof-v2.json \
  --allow-incomplete-cost
```

결과는 `api_cost_complete:false`와 unknown attempt를 출력한다. duplicate attempt,
native ownership, digest/signature, quality/memory/runtime, usage 산술과
profile/MLflow/artifact linkage를 검사한다. 이것은 active-profiling 비교 auditor나
독립 confirmation 실험을 대체하지 않는다. 기존 BO null result와 profiling
비용 회수 실패 결과는 변경하지 않았다.

## 구현과 검토

[bounded runner](../src/resource_advisor/npu_probe.py),
[immutable renderer](../examples/build_npu_target.py),
[fingerprint 정책](../src/resource_advisor/policy.py),
[operator allowlist backend](../src/resource_advisor/backends.py),
[배포·복구 설명](../deploy/npu/README.md)을 연결한다. 기존 API/DB schema는 유지했다.

Standards review: capability age를 실제 timestamp로 보존하고 SDK 실패에도 helper를
정리하도록 수정했다. Spec review: operational observe와 performance reuse 경계를
분리했고 기본 backend 보안 설정을 유지했다. 두 독립 review에서 추가 중대 결함은
없었으며 host proxy attestation·불완전 firmware·lab isolation 한계를 유지한다.
Focused software tests와 real hardware 성공은 별도 증거다.
최종 focused tests는 108개 통과했다. 기존 full-suite 결과를 이번 변경의 재실행으로
표현하지 않는다. ArgoCD services/worker는 Synced·Healthy이며 Console 기본 목록의
새 템플릿은 manual submission 가능 상태로 확인했다. 종료한 소유 lab Pod21개를
로그·UID 보존 후 정리했고 잔여0이다.
[정리 증거](evidence/all-accelerators-20261008-cleanup-v1.json)를 별도로 남겼다.
추가 auditor review에서 native 실패 receipt와 시간 변조의 false acceptance를 찾았고
수정했다. 회귀검사2개를 추가한 auditor replay8개가 통과했다. 원본 hardware evidence와
재계산 요약 숫자는 변경하지 않았다.
