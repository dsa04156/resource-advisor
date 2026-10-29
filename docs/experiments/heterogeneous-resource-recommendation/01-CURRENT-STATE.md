# 01. 현재 상태 요약 — 이기종 자원 추천

2026-10-29 Phase 1 읽기 전용 감사의 요약이다. 코드 근거·관측 방법·증거 해시는 [CURRENT-STATE-AUDIT.md](CURRENT-STATE-AUDIT.md)를 본다.
구현·실험은 시작하지 않았다.

## 목표

측정 profile + native queue 상태 + 네트워크/데이터 위치로 GPU·NPU·CPU 후보 중 실행 대상을 추천한다. Kueue는 admission·quota, kube-scheduler는 최종 노드 배치, Slurm은 자체 할당을 맡는다. 현재 기본 동작인 노드 pinning은 유지하고, native scheduler가 노드를 고르는 pool mode는 향후 opt-in 기능이다. 사용자가 확정한 B0–B5·E1–E8을 새로 사전 등록해 검증하며, 우월성은 미리 정하지 않는다. 2026-10-28 live-training은 과거 기록이다.

## 지금 있는 것

- **SchedulingProfile/`compile_plan`**: 호환성·정책 필터 → request headroom(available/busy/unknown) → backend 선호 → 진행 중·최근 5분 접수 수 → ref 순의 결정적 규칙. 성능 profile·queue 대기·데이터 위치는 쓰지 않는다. observe 모드 전용.
- **노드 고정**: 후보 = capability 1개 = 노드 1개. Kubernetes는 hostname nodeSelector, Slurm은 `--nodelist`를 항상 넣는다. Kueue는 admission·quota만 결정하고 kube-scheduler가 그 고정 노드로 bind한다. native scheduler가 여러 노드 중 고르는 pool routing은 없다.
- **실측 추천 `recommend`**: 같은 workload/context 서명의 hardware profile 평균 elapsed, drift gate, baseline 보존, 15분 유효, 명시적 승인. compile_plan과 별도 경로.
- **실험용 `jct_selection`**: 정확한 identity·TTL(profile 1일, queue 5초)·quality/synthetic gate·provenance, abstain 지원. 운영 경로 미연결.
- **추론 라우터**: CUDA/Hailo ResNet50 상시 endpoint의 inflight×EWMA 선택. 배치 Job 추천과 별개.
- **`placement.py`**: 측정 링크 기반 전송 비용 preview. 미연결.
- **실행·결과 원장**: submit intent, `SUBMISSION_UNKNOWN` reconcile, 결과 서명·품질 검증, usage ledger, MLflow/S3 — 그대로 재사용.

## 없는 것 (신규 작업)

1. precision·compiled artifact가 다른 GPU/NPU variant를 같은 작업으로 묶는 **작업 family 계약**. 현재는 `MODEL/LOGICAL_WORKLOAD/PRECISION_MISMATCH`로 한 workload에 넣을 수 없다.
2. `compile_plan` 정렬에 측정 profile·queue를 넣는 **ranker hook**과 DB profile/queue → `jct_selection` 입력 **adapter**.
3. 명시적으로 선택할 때만 hostname 대신 label 제약을 주고 kube-scheduler/Slurm이 노드를 고르게 한 뒤, 실제 노드를 capability에 결합하는 **opt-in pool mode** (기본은 pinning 유지).
4. **데이터 위치** 계약 필드와 전송 비용 항목. 전송 시간은 순차·비중첩이 입증된 구간에서만 더하고, 병렬 구간은 critical path로 계산하며 중복 합산하지 않는다.
5. 추천 입력에서 operational mode의 stale capability 완화를 끄는 **신선도 guard**.

## 과거 결과에서 지켜야 할 것

BO·전이 우위 없음, profiling 비용 미회수(N≤3, 6회). JCT 162작업에서 profile+queue가 평균 JCT를 개선하지 못함(6/54 fallback, 감사 중단). profile-pool v2는 compute −72%였지만 JCT +3.3%, 대기 +320%. 첫 추론 초기화 약 9.5초 vs 본 계산 약 2.7초(n=3/cell). 혼합 GPU/NPU 실험은 예산 소진으로 미완료.

## 오늘 장비 (2026-10-29 UTC, 읽기 전용)

관측 시각: Kubernetes snapshot 시작 02:29:16Z, physical Slurm probe 시작 02:31:02Z, Slurm VM 02:31:48–02:31:56Z, Prometheus 02:32:08–02:32:15Z. 그 밖의 개별 `kubectl get` 시각은 unknown.

| 상태 | 후보 |
|---|---|
| AVAILABLE (resource 노출, 오늘 runtime 실행은 미검증) | Orin Nano A, Hailo8 B, Mobilint ARIES2, Intel NPU, Kubernetes CPU, Slurm VM CPU |
| AVAILABLE이지만 queue route BLOCKED | Orin-class B (Kueue flavor 없음) |
| BLOCKED | RTX5060Ti·RTX5080·GB10·Hailo8 A (다른 project Deployment가 점유), DEEPX (resource 없음), HAMi (scheduler/plugin 미가동, 잔여 annotation만 있음) |
| UNREACHABLE | AGX Orin (02:24:25Z부터 Ready=Unknown, Kueue flavor가 없는 hostname을 가리킴), RK3399Pro (10-19부터), physical Slurm controller·Orin·Pi (TCP timeout) |
| 장치·runtime UNVERIFIED / route UNSUPPORTED | AMD/ROCm (광고된 resource·plugin 없음 = 현재 route 없음. 장치 비호환 증거는 아님) |

Kueue 큐는 모두 비어 있고 활성 RA Job은 0이다. RA는 operational mode이고 inventory collector·GPU worker·추론 endpoint는 replica 0이다.

## 실행 제약

- 실패한 Astra 작업은 공식 문서 조사 preflight였다(코드 리뷰 아님). Run `run_86457554ce2f`, dispatch `ctx_f275e857fdbd`, 오류 `Agent startup blocked: agent-hooks-review-prompt`. 독립 리뷰는 실행되지 않았고, hook 검토가 해결될 때까지 후속 독립 리뷰를 보장할 수 없다. orchestration 제약이며 장비 문제가 아니다.
- 기존 software baseline 120 passed(coordinator 실행, 기존 테스트). 새 구현·하드웨어 증거가 아니다.

## 다음 단계 제안

Baseline(사용자 확정): B0 native/static, B1 Round Robin, B2 현재 RA, B3 Profile Only, B4 Profile+Queue, B5 Profile+Queue+Network.
실험(사용자 확정): E1 단일 GPU 학습, E2 sparse/moderate/burst 동시 학습, E3 같은 데이터 local/remote, E4 DDP/NCCL 또는 NOT_VALIDATED, E5 동등 NPU 추론, E6 학습 guard를 우회하지 않는 Slurm 공통 결과, E7 stale/runtime/queue/network/mismatch 신뢰성, E8 profiling overhead/ROI.

1. seam 1–6 구현(별도 dispatch). 과거 기록 offline replay는 그 software 준비이며 어떤 E 결과도 아니다(새 E1 결과 아님).
2. 오늘 장비로 **할 수 없는 것**: E1·E2(GPU 모두 점유, Jetson 학습 variant 없음), E3(데이터 위치 계약·측정 링크 없음), E4(다중 GPU 없음 → NOT_VALIDATED 예상), E5의 GPU 비교 쪽, E6의 GPU/NPU Slurm(physical Slurm unreachable, VM은 CPU만), AGX·RK3399Pro·AMD·DEEPX·HAMi 측정. 결과를 만들어 넣지 않는다.
3. 부분 가능: E5(Hailo8 B·Mobilint·Intel NPU, 작업 family 계약 필요), E6(Slurm VM CPU), E7(stale·mismatch). E8은 앞 실험 데이터에 종속된다.
4. 예산 숫자(Job ≤60, wall ≤30분, slot-초 ≤1,800)는 제안일 뿐이며 고정 계획이 아니다. 점유된 GPU와 운영 큐/flavor 변경은 소유자·사용자 승인 후에만 사용한다.
