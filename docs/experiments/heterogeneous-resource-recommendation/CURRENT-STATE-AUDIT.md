# 이기종 GPU·NPU·CPU 자원 추천: 현재 상태 감사 (Phase 1)

감사일: 2026-10-29 (UTC). 대상: `resource-advisor` main `2a528dd` + 미커밋 작업 트리.
이 문서는 **읽기 전용 감사**다. 소스·테스트 변경, 벤치마크, Job 제출, 배포, quota·label·QOS 변경은 하지 않았다.
요약판은 [01-CURRENT-STATE.md](01-CURRENT-STATE.md)다.

## 0. 이번 목표 (갱신된 목적)

이전 목표였던 live-training(2026-10-28) 실행은 **과거 기록**이다. 이번 프로젝트의 목표는 다음과 같다.

- 측정된 실행 profile + native queue 상태 + 네트워크/데이터 위치를 사용해 GPU·NPU·CPU 후보 중 실행 대상을 **추천**한다.
- 역할 경계: Kueue는 admission·quota, kube-scheduler는 Kubernetes Pod의 최종 노드 배치, Slurm은 자체 할당을 담당한다. **현재 기본 동작은 노드 pinning이며 그대로 유지한다.** native scheduler가 노드를 고르는 pool mode는 향후 opt-in 기능이다.
- 사용자가 확정한 baseline B0–B5와 실험 E1–E8(§7)을 새로 사전 등록·실행해 재현 가능한 증거와 NAIS 발표 자료를 만든다.
- **우월성을 미리 정하지 않는다.** 개선이 없으면 그 결과를 그대로 보고한다.

이 목표는 [PROJECT-PURPOSE.md](../../PROJECT-PURPOSE.md)에도 반영했다. 목표/수용 기준이지 완료 선언이 아니다.

### 증거 수준 표기

`코드 존재` / `software test 통과` / `실장비 과거 기록` / `오늘(2026-10-29) 읽기 전용 관측` / `미검증`을 구분한다.
과거 로그의 성공을 현재 사용 가능 상태로 해석하지 않는다.

## 1. 초기 작업 트리와 보존 상태

- HEAD `2a528dd` (main). 초기 상태 기록 시각 2026-10-29T02:27:02Z(비공개 `00-initial-timestamp.txt`).
- `git status --porcelain -uall` 103행: 수정 6, 삭제 44, untracked 53.
  - 삭제 44개는 `docs/evidence/*`·`examples/verify_*` 등으로, untracked [`docs/archive-manifest.json`](../../archive-manifest.json)이 원래 경로·SHA256·baseline commit `02791f5`를 기록한다. 이 정리 작업은 이번 감사 범위 밖이며 되돌리지 않았다. 일부 기존 문서(예: `right-sizing-hardware-matrix.md`의 readiness 링크)는 작업 트리에서 링크 대상이 없을 수 있다.
  - 수정: `docs/README.md`, `docs/nais-interview/START-HERE.md`, `examples/scheduler_lab/{agent.py,config.example.json,priority-resources.json}`, `tests/test_scheduler_lab_runner.py` (scheduler lab의 단계별 aging 우선순위).
  - Untracked 중 이기종/프로파일 재사용 관련: `src/resource_advisor/profile_reuse_workload.py`, `examples/{run,analyze,finalize,monitor,prepare}_profile_reuse*.py`, `docs/{existing-profile-experiments,heterogeneous-profiling-next-research,profile-reuse-research,profile-reuse-repository-understanding}.md`, `docs/evidence/profile-reuse-20261008-v1-*`. VLM LoRA(`vlm_lora*.py`, `tests/test_vlm_lora*.py`, `examples/vlm_lora/`)는 선택 워크로드다.
- 수정·untracked 파일 59개의 SHA256, 초기 status, tracked diff는 비공개 scratch에 보존했다(§9).
- 이번 감사가 추가·수정한 파일은 이 디렉터리의 두 문서와 `docs/PROJECT-PURPOSE.md`뿐이다.

## 2. 기존 기능: 실제 동작 (코드 근거)

### 2.1 SchedulingProfile / `compile_plan`

[`scheduling.py`](../../../src/resource_advisor/scheduling.py)

- 정책: `SchedulingPolicy`(backend 순서, 허용 작업·할당 유형, 자원 상한, normal/high, 대기·실행 한도). `quota_scope=global`과 preemption override는 거절(`compile_plan` 219–222행).
- 후보는 **하나의 `WorkloadSpec` 안의 `candidates`**뿐이다(225행). 각 후보를 `execution_compatibility`(policy.py 97행), 승인 필요, backend·할당 유형·자원 상한, Slurm 학습 격리, binding·priority 지원으로 거른다(251–314행).
- 정렬(324–333행): `available → busy → unknown` → `backend_order` → 같은 project의 진행 중 접수 수 → 최근 5분 접수 수(236행) → candidate ref. **가중 점수·실행시간 예측·queue 대기 예측·데이터 위치는 사용하지 않는다.**
- `availability`(150–181행)는 fresh inventory의 scheduler **request headroom**을 본다. GPU 사용률이 아니다. 관측이 없으면 `unknown(NO_FRESH_INVENTORY)`.
- `plan_digest`는 `evaluation`을 제외하고 서명한다(184–186행). 고정 후보 제출은 digest 재검증, 자동 제출은 접수 시 재평가 후 저장한다.
- 자동 선택은 PostgreSQL project advisory transaction lock으로 직렬화된다([service.py](../../../src/resource_advisor/service.py) 145행). 물리 GPU 예약이나 project 간 quota가 아니다.
- **profile 적용 범위:** 자동 선택과 scheduling profile은 `mode=observe`이고 approval/study/probe가 없을 때만 허용된다(service.py 160–170, 216–226행). 즉 **추천·승인 실행 경로와 compile_plan 경로가 분리**되어 있다.
- `verify_adapter_plan`(382행)이 실제 adapter binding과 저장된 plan의 차이를 `SCHEDULING_BINDING_DRIFT`로 막는다.

### 2.2 노드 pinning vs pool routing

- 각 후보는 `Candidate.capability_ref` → `CapabilitySnapshot.node_ref` 하나에 묶인다([contracts.py](../../../src/resource_advisor/contracts.py) 197–228행).
- Kubernetes: `nodeSelector["kubernetes.io/hostname"] = capability.node_ref`를 **항상** 넣는다([backends.py](../../../src/resource_advisor/backends.py) 295–299행, `spec.nodeName`은 쓰지 않음). Job은 `suspend: true` + Kueue queue/priority label(317행 부근).
- Slurm: `--nodelist=<node_ref>` + `--gres`(837, 851행).
- 따라서 현재 "자동 선택"은 **상위 계층이 노드를 고른다.** Kubernetes에서는 Kueue가 그 Job의 admission·quota(ResourceFlavor 할당 포함)를 결정하고, kube-scheduler가 hostname selector를 만족하는 그 노드로 Pod를 bind한다. Slurm은 지정된 노드에서 할당한다. kube-scheduler나 Slurm이 동등한 여러 노드 중 고르는 pool routing은 **없다**. Kueue `hairp-gpu-pool` ClusterQueue는 quota를 공유하지만 Pod는 여전히 hostname으로 고정된다([common-gpu-pool.md](../../reference/common-gpu-pool.md)). Kueue flavor의 node label은 배치 제약을 더할 뿐이며 최종 노드 선택은 kube-scheduler가 한다.
- 실제 노드 관측은 재사용 가능: Kubernetes status가 Pod `spec.nodeName`을 읽고(backends.py 547행 부근), Slurm은 할당 노드가 capability와 다르면 거절한다(1176행 부근).

### 2.3 Request headroom

- inventory snapshot의 노드별 `resources.<key>.request_headroom`을 CPU·memory·가속기 resource key 단위로 비교한다(scheduling.py 169–181행).
- 부족하면 `busy`로 남겨 native queue에서 기다리게 한다. down/drain/fail 관측은 제외한다.
- **한계:** Kueue 밖에서 device plugin을 직접 쓰는 Pod(예: 다른 project Deployment)는 Kueue quota에는 보이지 않는다. headroom이 이를 반영하는지는 collector 구성에 달려 있고, 오늘 collector는 꺼져 있다(§4.2).

### 2.4 실측 추천 (`Service.recommend`)

- 같은 `workload_signature` + 같은 `context_signature` + `COMPLETED` + hardware evidence + `max_profile_age_seconds` 이내 profile만 사용(service.py 668–735행).
- 최근 창 drift gate → 부족하면 `NEEDS_PROFILE`/`NEEDS_RECONFIRMATION`(739행 부근).
- 평균 elapsed로 정렬, mean ± 3SE 서술 구간, baseline과 구간이 겹치면 `PRESERVE_BASELINE_UNCERTAINTY`(778행).
- 추천 유효시간 15분(791행), 항상 명시적 승인 필요. queue·데이터 위치·비용은 목적함수에 없다.

### 2.5 실험용 JCT 선택 (`jct_selection.py`)

[`jct_selection.py`](../../../src/resource_advisor/jct_selection.py) — 순수 함수, 운영 경로 미연결.

- **Identity:** `CandidateIdentity(candidate_ref, backend, cluster_ref, node_ref, resource_key, runtime_digest, workload_digest)`(23–32행). profile·queue·intent identity가 정확히 같아야 한다.
- **TTL:** profile 기본 86,400초, queue 5초(206–207행). 미래 시각·stale은 abstain(`PROFILE_STALE`, `QUEUE_STALE`).
- **품질·출처:** `qualified`, `quality_passed`, `synthetic=False` 필수(139–168행). `evidence_ref`, `queue_snapshot_ref`, `accounted_job_ids`, timing uncertainty를 예측에 보존.
- 정책: `round_robin`, `profile_only`(정적 backlog), `profile_queue`(완전한 queue snapshot + 미반영 submit intent로 FIFO 완료시간 추정). 근거가 없으면 `candidate_ref=None`으로 abstain하고 fallback은 호출자가 별도로 기록해야 한다.
- 사용처: `jct_evaluation.py`, `examples/run_jct_comparison.py`만. API·compile_plan에서 호출하지 않는다.

### 2.6 이기종 추론 라우터 (`inference_routing.py`)

[`inference_routing.py`](../../../src/resource_advisor/inference_routing.py)

- 같은 source model SHA의 CUDA/Hailo **상시 endpoint**를 하나의 pool로 묶고, readiness·identity(`EndpointBinding`, 27–55행) 확인 후 `(inflight+1) × max(service_ms, EWMA observed_ms) × count`가 가장 작은 endpoint를 고른다(280행, EWMA 348행). 실패 시 다른 endpoint 1회 재시도.
- runtime은 `cuda|hailo`만, ResNet50 RGB224 입력 계약만. 배치 Job 추천과는 별도 기능이다. 오늘 두 endpoint Deployment replica는 0이다(§4.2).

### 2.7 네트워크/토폴로지

[`placement.py`](../../../src/resource_advisor/placement.py)는 operator가 import한 `TopologySnapshot`의 **측정된 방향 링크**로 role-graph 전송 비용을 추정한다. 미측정 링크는 unknown이며 0으로 두지 않는다. **planning preview 전용**이고 compile_plan·recommend와 연결되지 않았다. 계약에 데이터 위치(dataset location) 필드는 없다.

### 2.8 Kubernetes/Kueue와 Slurm 실행·결과 계약

- 제출: worker가 원격 호출 **전에** `SUBMITTING`을 커밋([worker.py](../../../src/resource_advisor/worker.py) 119행). 응답 불명은 `SUBMISSION_UNKNOWN`(156행)이며 같은 attempt 이름으로 reconcile하고 맹목 재제출하지 않는다(backends.py `reconcile` 427, 910행).
- 결과: backend 완료 → `COLLECTING` → `ExecutionResult`(contracts.py 314행) 검증 → `ingest`(service.py 470행)에서 attempt/epoch·digest·work unit·quality 검사. `COMPLETED`만 measurements를 가진다. 예측은 결과가 아니다.
- Slurm: 해시 검증된 native runtime만, container variant는 preflight 거절, 학습은 CPU host-copy binding만([architecture.md](../../architecture.md)).
- 공통 profile·추천의 단위는 `elapsed_seconds`(workload가 측정한 compute 구간)이고, JCT·대기·예약초는 native timestamp에서 별도로 나온다. 서로 대체하지 않는다.

### 2.9 호환성 경계 (이기종 추천의 핵심 제약)

[`policy.py`](../../../src/resource_advisor/policy.py) `compatibility`:

- `MODEL_MISMATCH`, `LOGICAL_WORKLOAD_MISMATCH`, `PRECISION_MISMATCH`(58–60행): variant의 model digest·workload signature·precision이 `WorkloadSpec.identity`와 같아야 한다.
- 결과적으로 **FP32 CUDA와 INT8 Hailo HEF 같은 서로 다른 compiled precision은 한 WorkloadSpec의 후보가 될 수 없다.** 현재 GPU↔NPU 비교는 별도 workload로만 존재하고 문서상 `NOT_COMPARABLE`이다.
- `RUNTIME_FINGERPRINT_INCOMPLETE`(38–45행): Intel/Mobilint/Rockchip NPU는 firmware identity가 없으면 성능 재사용 보류.
- `execution_compatibility`(97–106행): **operational mode에서는 `CAPABILITY_STALE`과 `RUNTIME_FINGERPRINT_INCOMPLETE`를 무시**한다. 현재 API는 `RA_OPERATIONAL_MODE=1`(§4.2). 추천 근거에 이 완화가 섞이면 안 된다.
- `WorkloadSpec.approved_space`: 후보 간 accelerator count 변경 불가(contracts.py 287행).

## 3. 과거 실험: 부정·무효·미완료 결과

| 기록 | 결과 | 이번 설계에 주는 의미 |
|---|---|---|
| Static/Random/qLogNEI (W1/W2, RTX5080) | Random·BO가 같은 구성 선택, N≤3에서 profiling 비용 미회수 | BO 우위 없음. 탐색기를 새로 만들지 않는다 |
| 과거 B0/B1/B2 운영 비교 (이번 B0–B5 ID와 무관) | 연산시간 약 1% 감소, 6회까지 비용 미회수 | 단일 GPU right-sizing 효과는 작다 |
| Transfer/RGPE | 네 전략 같은 구성 | 전이 우위 없음 |
| MF-KG fidelity | 실제 GPU 그룹 qualification 실패 | 다중 fidelity 사용 불가 |
| JCT 162작업 (`jct-profile-queue-20261008-short-v1`) | RR 6.537 / Profile Only 6.389 / P+Q 6.556초 평균 JCT, P+Q 54건 중 6건 fallback, 최종 독립 감사 중단 | **queue-aware 선택이 평균 JCT를 개선하지 못했다.** 운영 경로 미반영 |
| profile-pool v2 (n=1/조건) | 동기화 추론 합 −72%, 그러나 평균 JCT +3.3%, 대기 +320% | compute 단축이 JCT로 이어지지 않음. 빠른 장치로 몰리면 대기 증가 |
| profile-reuse-20261008-v1 (RTX5080 1대) | JCT 차이 0.071%, 초기 비용 포함 시 비용 증가, DCGM≠NVML | 예비 실험. 성과 근거로 쓰지 않음 |
| 혼합 GPU/NPU closed-batch (2026-10-14, 비공개 `.state`) | 210 계획 중 203 제출, 예산 소진으로 protocol 미완료, 탐색적, 우월성 검정 없음 | 공개 증거 없음. 재실행 시 예산 고정 필요 |
| profile-causality (2026-10-14, 비공개) | 첫 추론 평균 약 9.5초 vs 본 계산 약 2.7초, n=3/cell | **초기화/준비 비용이 정적 compute profile보다 클 수 있다** |
| Hailo EfficientFormer 등 | 고정 품질 gate 실패 | 장치 노출 ≠ 모델 지원 |
| Reference v1/v2 | native `DeadlineExceeded`로 abstain | exit 0이라도 실패 Job을 성능으로 쓰지 않음 |
| 문헌(2026-10-15 정리) | Clockwork·LENS·BYSTANDER가 profile+queue-aware 선택을 이미 다룸 | "profile+queue 사용" 자체는 새 기여가 아니다 |

## 4. 현재 장비 상태 (오늘 읽기 전용 관측)

관측 시각(비공개 raw 기록 기준, UTC 2026-10-29): Kubernetes snapshot 시작 02:29:16Z(이후 deployments 등 추가 `kubectl get`의 개별 시각은 기록하지 않아 unknown), physical Slurm probe 시작 02:31:02Z(호스트별 완료 시각 unknown), Slurm VM status 02:31:48Z·queue 02:31:56Z, Prometheus 응답 sample 02:32:08–02:32:15Z.
방법: `kubectl get`(nodes/pods/jobs/Kueue CRD/daemonsets/deployments), Prometheus instant query, TCP connect + BatchMode SSH(physical Slurm), 기존 `deploy/slurm-vm/status.sh`와 VM 내부 `squeue`/`sacct`.
**Kubernetes Ready와 advertised resource는 runtime 실행 성공이 아니다.** 오늘 실행한 장치 workload는 0개다.
노드 이름·주소는 alias로 표기하고 원본 대응표는 비공개 scratch에 둔다.

| 후보 | 상태 | 오늘의 근거 | 모르는 것 |
|---|---|---|---|
| RTX5060Ti (S1, amd64) | **BLOCKED** | Ready, DCGM `modelName=RTX 5060 Ti`, `nvidia.com/gpu` 1/1이 다른 project(edgeai) Deployment Pod에 할당(2026-10-28T03:47Z부터). 순간 util 0 | 점유 해제 시점, 오늘 runtime 실행 |
| RTX5080 (S2, amd64) | **BLOCKED** | Ready, DCGM `modelName=RTX 5080`, GPU 1/1 edgeai 점유(2026-10-22부터), FB used 848MiB | 동일 |
| GB10 (S3, arm64) | **BLOCKED** | Ready, spark exporter 성공, GPU 1/1 edgeai 점유(2026-10-22부터) | 오늘 exporter에 모델명 label 없음 → 모델명은 과거 flavor/문서 기준 |
| Orin Nano A (E-Nano-A) | **AVAILABLE** (resource) | Ready, `nvidia.com/gpu.shared`=2 미할당, jetson exporter 성공, Kueue flavor 있음 | runtime 실행 UNVERIFIED |
| Orin-class B (E-Nano-B) | **AVAILABLE** (resource) / route **BLOCKED** | Ready, gpu.shared=2 미할당, 별도 device plugin, Ubuntu 24.04 계열. 어떤 Kueue flavor도 이 노드를 가리키지 않음 | 정확한 모듈 모델, 과거 Slurm Orin과의 동일성 UNVERIFIED |
| AGX Orin (E-AGX) | **UNREACHABLE** | Ready=Unknown, 전이 2026-10-29T02:24:25Z. Kueue flavor `hairp-pool-jetson-b`/`ra-jetson-agx`는 클러스터에 없는 다른 hostname을 가리킴(flavor drift) | 복귀 여부 |
| Slurm Orin (physical) | **UNREACHABLE** | 기록된 주소의 22/6817/6818/6819 TCP timeout(probe 시작 02:31:02Z) | 주소 변경 여부 unknown |
| Slurm controller / Pi CPU (physical) | **UNREACHABLE** | 동일 timeout | 동일 |
| Slurm VM (CPU) | **AVAILABLE** | QEMU TCG 실행 중(14h52m), munge/mariadb/slurmdbd/slurmctld/slurmd active, Slurm 24.11.5, 1노드 idle 2CPU/3072MB, 큐 비어 있음, 최근 24h Job 0 | 성능은 TCG라 실서버와 비교 불가 |
| Hailo8 A (E-Hailo-A) | **BLOCKED** | `hailo.ai/h8` 1/1 edgeai 점유(2026-10-22부터) | — |
| Hailo8 B (E-Hailo-B) | **AVAILABLE** (resource) | Ready, h8=1 미할당, device plugin ready | 오늘 HEF 실행 UNVERIFIED |
| Mobilint ARIES2 (S4) | **AVAILABLE** (resource) | `mobilint.com/npu`=1 미할당, exporter: driver 1.14.0, firmware 1.2.4, util 0 | exporter firmware 값과 runtime manifest 결합은 UNVERIFIED |
| Intel NPU (S2 호스트) | **AVAILABLE** (resource) | `npu.intel.com/accel`=1 미할당, exporter 성공 | 같은 호스트 GPU가 다른 project에 점유됨 → CPU/memory 간섭 가능, firmware identity 미결합 |
| RK3399Pro (E-RK) | **UNREACHABLE** | Ready=Unknown, 전이 2026-10-19T04:15:36Z, device plugin desired 0 | — |
| AMD/ROCm | 장치·runtime **UNVERIFIED** / 현재 route **UNSUPPORTED** | 어떤 노드에도 `amd.com/gpu` resource·ROCm plugin이 광고되지 않음 → 현재 실행 route가 없다는 뜻이며 장치 비호환 증거가 아님 | 물리 장치 존재·ROCm 지원 여부 |
| DEEPX | **BLOCKED** | extended resource·plugin 없음. 과거 PCIe 미탐지 기록이 있던 Slurm Pi는 오늘 unreachable | 현재 장착 여부 UNVERIFIED |
| CPU (Kubernetes) | **AVAILABLE** | 서버·Pi5 노드 Ready, `ra-live-training-cpu` 큐 존재 | S1에는 RA API·다른 project Pod가 같이 있음 |
| HAMi | **BLOCKED** | 11노드에 `hami.io/node-handshake=Requesting_2026-10-28 02:16:50`–`02:17:12`(annotation 기록값) 잔여 annotation, S2 `hami.io/vgpu`=0, HAMi scheduler/device-plugin Pod·Deployment 없음 | HAMi 간섭 비교 불가 |

### 4.1 운영 작업 점검 (실험 전 필수)

- Kueue ClusterQueue 15개 모두 pending 0 / admitted 0. Workload 1,457개 모두 Finished(lab 1,454, team-b 3). 활성 RA Job 0.
- 다른 project `edgeai` namespace의 Deployment Pod(default-scheduler, Kueue 미사용)가 **세 개의 exclusive NVIDIA GPU 전부와 Hailo8 A**를 점유한다. Kueue quota에는 보이지 않으므로 Kueue가 admission해도 Pod가 Pending/FailedCreate될 수 있다. 이 Pod들은 중지·이동하지 않는다.

### 4.2 Resource Advisor 배포 상태 (오늘)

- `ra-api` 1 replica, `RA_OPERATIONAL_MODE=1`. `ra-live-training-worker`·`ra-live-training-slurm-worker` 각 1 replica Running(이전 live-training 경로).
- `ra-inventory`, `ra-slurm-inventory`, `ra-worker`, `ra-slurm-worker`, `ra-infer-cuda-v1`, `ra-infer-hailo-v1`, `ra-serving-monitor`는 replica 0.
- 따라서 지금 compile_plan의 availability는 대부분 `unknown(NO_FRESH_INVENTORY)`이고, operational mode가 stale capability를 허용한다. 이 상태의 선택 결과를 추천 성능 근거로 쓰면 안 된다.

### 4.3 실행 환경 제약 (장비 상태와 별개)

- 실패한 Astra 작업(GPT-6)은 **공식 문서 조사 preflight**였고 코드 리뷰가 아니다. Run `run_86457554ce2f`, 실패 dispatch `ctx_f275e857fdbd`, `agent_readiness` 단계 오류 `Agent startup blocked: agent-hooks-review-prompt`(새/변경 Codex hook 4개의 사용자 검토 필요).
- 독립 리뷰는 **실행되지 않았다.** hook 검토가 해결되기 전에는 후속 독립 리뷰의 실행을 보장할 수 없다. 이는 **orchestration 실행 제약**이며 하드웨어 unavailability가 아니다. 이번 턴에는 구현을 시작하지 않는다.
- 기존 software baseline: coordinator가 `rtk proxy uv run --locked pytest -q tests/test_scheduling.py tests/test_jct_selection.py tests/test_inventory.py tests/test_slurm_inventory.py tests/test_queue_inventory.py`를 실행해 **120 passed, 3 warnings, 0.59s, exit 0**을 보고했다. 기존 코드의 software 테스트이며 새 구현이나 하드웨어 증거가 아니다. 이 감사 작업자는 테스트를 다시 실행하지 않았다.

## 5. 재사용 / 신규 작업 지도

| 필요 기능 | 재사용 (현재 존재) | 없는 것 (신규 작업) |
|---|---|---|
| 후보 적격성 | `compatibility`/`execution_compatibility`, binding·자원 상한·우선순위 검사 | 추천 경로에서 operational staleness 완화를 끄는 옵션 |
| GPU↔NPU 동등 작업 | `combine_workload.py`(identity 동일 시만), 추론 라우터의 source-model SHA·입출력 계약 | precision·compiled artifact가 다른 variant를 같은 **작업 family**로 묶는 계약과 family 단위 품질 gate. 현재 `WorkloadIdentity` 서명은 바꾸지 않아야 함 |
| 측정 profile | `ingest`·`lookup_profiles`·drift gate, `MeasuredProfile` identity/TTL/quality/provenance | DB profile → `MeasuredProfile` 변환기, 준비/초기화 비용 구간(profile-causality) 반영 |
| Queue 상태 | `QueueSnapshot`(complete 플래그, intent dedupe), Kueue/Slurm inventory collector 코드 | Kueue 밖 device-plugin 점유까지 포함한 완전성 판정, collector 재가동(배포 변경이므로 승인 필요) |
| 추천 점수 | `select_candidate`(profile_only/profile_queue, abstain) | `compile_plan` 정렬 단계에 연결하는 ranker hook, fallback 사유 기록, approval 경로와 통합 |
| 네트워크/데이터 위치 | `placement.py` 측정 링크 전송 비용 추정 | 데이터 위치 계약 필드, compile_plan 점수 항목 연결 |
| Native 배정 | Kueue suspended Job(admission·quota), kube-scheduler bind, Slurm partition/QOS/GRES, Pod `nodeName` 관측, Slurm 노드 불일치 거절 | **opt-in pool mode**: hostname 대신 운영자 label 제약만 주고 kube-scheduler/Slurm이 노드를 고름, 배정 후 실제 노드 → capability 결합 검증. 기본은 pinning 유지 |
| 실행·결과 원장 | submit intent, reconcile, result 검증, usage ledger, MLflow/S3 | 없음(그대로 재사용) |
| 실험 하네스 | `jct_evaluation.py`, `run_jct_comparison.py`, profile-reuse runner/analyzer, 감사기들 | 새 B0–B5/E1–E8 사전 등록 plan과 감사기 |

## 6. 최소 구현 seam (제안, 미구현)

1. **Ranker hook** — `scheduling.py` 324–333행의 정렬을 `rank(eligible, evidence)`로 분리. 기본 구현은 현재 순서와 비트 단위 동일(기존 테스트 유지). 새 ranker는 `jct_selection.select_candidate`를 호출하고, abstain이면 기존 순서로 내려가며 `fallback_reason`과 입력 snapshot ref를 plan `evaluation`에 저장한다(digest 대상 밖).
2. **Evidence adapter (새 모듈)** — 저장된 `profile` entity → `MeasuredProfile`, inventory/queue snapshot → `QueueSnapshot`. Kueue 밖 점유를 모르면 `complete=False`로 abstain.
3. **Opt-in pool target mode (향후)** — plan에 `target_mode: node | pool`. `node`는 현재 동작이며 **기본값으로 유지**한다. `pool`은 명시적으로 선택한 경우에만, Kubernetes에서 hostname selector 대신 운영자가 등록한 label 제약을 주어(backends.py 295–299행) Kueue는 admission·quota를, kube-scheduler는 최종 노드 배치를 하게 한다. Slurm은 `--nodelist`를 생략한다(837행). 배정 후 관측 노드를 compatible capability에 결합하고, 맞지 않으면 결과를 profile로 쓰지 않는다.
4. **작업 family 계약** — 여러 `WorkloadSpec`(장치별 precision/artifact)을 공통 입력·출력·품질 gate로 묶는 새 contract. 같은 family 안에서만 비교하고 그 외는 `NOT_COMPARABLE`.
5. **데이터/네트워크 항목** — `placement.py`의 측정 링크 추정을 사용한다. 전송 시간은 **순차적이고 서로 겹치지 않음이 측정 경계로 입증된 구간에서만** 다른 구간에 더한다. 전송과 계산·준비가 병렬로 겹치면 critical path(가장 긴 경로)로 계산하고 겹친 시간을 중복 합산하지 않는다. 겹침 여부를 모르면 합산하지 않고 unknown → abstain. 미측정 링크도 unknown → abstain.
6. **추천 신선도 guard** — ranker 입력에는 operational staleness 완화를 적용하지 않는다.

각 seam은 기존 서명·digest·idempotency를 보존해야 한다. 수용 기준은 별도 구현 dispatch에서 확정한다.

## 7. 확정 baseline·실험과 현재 장비로 가능한 범위 (미실행)

ID와 의미는 사용자가 확정했다. 이 문서는 ID를 바꾸지 않는다. 아래 장비 판단은 §4의 2026-10-29 관측 기준이며, 어떤 실험도 아직 실행하지 않았고 결과도 없다.

### Baseline (사용자 확정)

| ID | 정책 |
|---|---|
| B0 | native/static (사용자가 고정한 실행 대상, native scheduler 그대로) |
| B1 | Round Robin |
| B2 | 현재 Resource Advisor (`compile_plan` 기존 규칙) |
| B3 | Profile Only |
| B4 | Profile + Queue |
| B5 | Profile + Queue + Network |

### 실험 (사용자 확정)과 현재 장비 제약

| ID | 내용 | 오늘 장비로 가능한가 | 막는 요인 |
|---|---|---|---|
| E1 | 단일 GPU 학습 | **불가 (BLOCKED)** | exclusive GPU 3대 모두 다른 project 점유. Orin Nano A shared slot은 노출됐지만 `TRAINING_VERIFIED` GPU 학습 variant·학습 격리 binding이 없다 |
| E2 | sparse/moderate/burst 동시 학습 | **불가 (BLOCKED)** | 여러 GPU 필요. CPU 학습(K8s CPU, Slurm VM)만으로 실행하면 GPU E2가 아니라 CPU 범위로 따로 표기해야 한다 |
| E3 | 같은 데이터의 local/remote 위치 비교 | **불가 (BLOCKED)** | 데이터 위치 계약 필드 없음, 측정된 `TopologySnapshot` 링크 없음 |
| E4 | DDP/NCCL | **NOT_VALIDATED 예상** | 2개 이상 exclusive GPU 필요(모두 점유). Jetson shared slot은 다중 GPU DDP가 아니다 |
| E5 | 동등 NPU 추론 | **부분 가능** | Hailo8 B·Mobilint·Intel NPU는 resource 노출(오늘 runtime 미검증). GPU 비교 대상은 점유 중. precision이 다른 variant를 묶는 작업 family 계약(§6 seam 4) 없음. Intel/Mobilint는 firmware identity 미결합 |
| E6 | Slurm 공통 결과 (학습 guard 우회 금지) | **CPU만 부분 가능** | physical Slurm unreachable. Slurm VM은 CPU 전용이며 Slurm 학습 guard는 CPU host-copy binding만 허용한다. GPU/NPU Slurm 경로는 불가 |
| E7 | stale/runtime/queue/network/mismatch 신뢰성 | **부분 가능** | stale·mismatch는 software 테스트와 사용 가능한 장치로 가능. network 장애 주입·운영 큐 변경은 승인 필요 |
| E8 | profiling overhead/ROI | **E1/E2/E5 결과에 종속** | 위 실험이 막혀 있으면 계산할 원시 데이터가 없다 |

현재 장비로 할 수 없는 작업 목록: GPU 단일·동시 학습(E1, E2), local/remote 데이터 위치 비교(E3), DDP/NCCL(E4), GPU↔NPU 동등 비교의 GPU 쪽(E5 일부), Slurm GPU/NPU 실행(E6 일부), AGX·RK3399Pro·physical Slurm 경로, AMD/ROCm·DEEPX·HAMi 관련 측정. 이 항목에 결과를 만들어 넣지 않는다. 장비가 풀리지 않으면 `BLOCKED` 또는 `NOT_VALIDATED`로 보고한다.

### 별도 software 준비 (실험 아님)

과거 원시 기록(JCT 162작업, profile-pool v2, profile-causality, 혼합 pool)의 offline replay는 ranker 구현을 점검하는 **software 준비 작업**이다. 어떤 E ID의 결과나 증거로도 쓰지 않는다. 특히 새 E1 결과가 아니다.

### 예산 (제안일 뿐, 확정 계획 아님)

예시 상한: native Job ≤60, wall ≤30분, 가속기 slot-초 ≤1,800, 운영 Pod와 같은 장치 동시 사용 금지, 실패·미완료 비용 포함, 소진 시 즉시 중단. **이 숫자는 감사자 제안이며 고정된 실험 계획이 아니다.** 실제 plan은 실험별로 사전 등록하고 실행 전 SHA256으로 고정한다. 점유된 GPU, Kueue flavor/LocalQueue 추가(예: Orin-class B), quota·label 변경은 소유자·사용자 승인 후에만 사용한다.

## 8. 위험과 기존 대안

- profile+queue-aware 선택은 Clockwork, BYSTANDER, HASE, Sia 등에서 이미 다룬다. 기여 후보는 새 스케줄러가 아니라 **이기종 GPU/NPU/CPU에서 품질 계약을 지킨 추천과 native scheduler 경계의 증거, 실패 조건의 분리**다.
- Kueue의 다중 ResourceFlavor(admission·quota)와 kube-scheduler(최종 노드 배치)가 이미 pool 배정 기능을 제공한다. opt-in pool mode는 이를 재사용해야 하며 상위 계층이 노드 배정을 다시 구현하지 않는다.
- 과거에 queue-aware 선택이 평균 JCT를 개선하지 못했고, 빠른 장치로 몰려 대기가 늘었다. 같은 결과가 다시 나올 수 있다.
- 작은 testbed(장치당 1개, 다른 project 점유)라 일반화하면 안 된다.

## 9. 비공개 증거 위치

`/home/jinuk/codex-work/mlops/.scratch/heterogeneous-resource-recommendation-20261029/` (저장소 밖, 공개 금지):

- `00-initial-timestamp.txt`, `01-initial-git-status.txt`, `02-initial-diff-stat.txt`, `03-initial-sha256-modified-untracked.txt`, `04-initial-tracked-diff.patch`
- `raw/` — kubectl JSON, Prometheus 응답, `slurm-probe.json`(주소 포함), Slurm VM status/queue
- `05-raw-evidence-sha256.txt` — raw 파일 37개 SHA256. 예: `k8s-nodes.json` `9633617b…4d54`, `k8s-pods.json` `54871a5b…84b0`, `slurm-probe.json` `d92e931e…12a6`, `slurm-vm-status.txt` `b39897b6…dc2b`
- `06-alias-map-PRIVATE.md` — alias ↔ 노드 이름
- `scripts/slurm_probe.py` — 읽기 전용 probe(제출·설정 변경 명령 없음)
