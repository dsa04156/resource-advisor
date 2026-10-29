# 06. 추천 알고리즘 (`hetero-rec-v1`)

## 1. 정책 선택 (opt-in)

`SchedulingProfile.policy`에 추가:

```json
{
  "recommendation": {
    "policy": "profile_only | profile_queue | profile_queue_network",
    "max_profile_age_seconds": 86400,
    "max_queue_age_seconds": 30,
    "topology_snapshot_ref": "<B5에서만 필수>",
    "fallback": "none | legacy"
  },
  "target_mode": "node | pool"
}
```

| Baseline | 구현 |
|---|---|
| B0 native/static | 고정 `candidate_ref` + legacy profile (기존 경로) |
| B1 Round Robin | runner가 고정 후보 순서를 순환하며 B0 경로로 제출 |
| B2 현재 RA | `recommendation` 없음 (기존 `compile_plan` 순서) |
| B3 | `profile_only` |
| B4 | `profile_queue` |
| B5 | `profile_queue_network` |

## 2. 순서

1. hard filter 통과 후보만 순위 대상이다([02 §2](02-ARCHITECTURE.md#2-요청--결정--native-job-연결)).
2. 각 후보의 identity = `(candidate, backend, cluster, route = node 또는 pool ref, resource key, context signature, workload signature)`.
3. profile: [04 §3](04-PROFILING-METHODOLOGY.md#3-재사용-조건-ranker-입장-시점) 조건을 통과한 최신 `JctProfile`.
4. 예측:
   - B3: `JCT = critical_path(phases)` (queue·admission 제외)
   - B4: `JCT = queue_wait + admission(or readmission) + critical_path(phases)`
   - B5: B4 + `data_staging`을 측정 링크 전송 추정으로 대체
   - `queue_wait`: 최신 `RouteQueueSnapshot`(complete·fresh·identity 일치)의 running 잔여시간과 pending을 FIFO slot에 배치하고, snapshot에 아직 안 보이는 같은 route의 접수된 RA job(intent)을 더한다(같은 ID는 한 번만). 기존 `select_candidate` 계산을 그대로 쓴다.
5. 최소 예측 JCT, 동률은 candidate ref. 결과는 입력 순서와 무관하다.
6. 근거 부족:
   - 모든 사유가 `PROFILE_*` → `NEEDS_PROFILE`
   - 그 밖(`QUEUE_*`, `RUNNING_*`, `NETWORK_*`, `STAGING_NOT_SEPARABLE`, …) → `ESTIMATE_UNAVAILABLE`
   - `fallback=legacy`이면 기존 순서로 선택하고 `status=LEGACY_FALLBACK`, `fallback.reason`을 plan에 서명된 상태로 남긴다. `none`이면 plan을 만들지 않는다.

## 3. 관측 불가 상태의 처리

| 상황 | 처리 |
|---|---|
| Kueue 밖 장치 점유(다른 project Deployment) | collector가 occupancy unknown → snapshot `complete=false` → B4/B5 abstain |
| queue snapshot 없음·stale·미래 | `QUEUE_MISSING`/`QUEUE_STALE`/`QUEUE_FROM_FUTURE` |
| 기존(legacy) RA job이 같은 route에 있고 occupancy가 고정되지 않음 | `QUEUE_WORK_UNKNOWN` |
| 실행 중 작업이 예상 시간을 넘김 | `RUNNING_EXPECTATION_EXCEEDED` |
| 선행 작업 뒤 재입장 시간 미측정 | `READMISSION_UNKNOWN_OR_INVALID` |
| logical slot 용량 | `capacity_unit=logical_slot`으로 보존, 물리 장치 수로 해석하지 않음 |

## 4. 하지 않는 것

- 보정된 신뢰도, 정밀도, 최적성 보장.
- 서로 다른 precision/compiled artifact를 같은 순위에 넣기(같은 `WorkloadSpec` 안의 후보만 비교).
- 전역 GPU 예약, 스케줄러 간 물리 장치 공동 소유, migration, KAI 통합.
- native admission 보장: 예측은 기대 FIFO 완료 시각이며 Kueue/Slurm의 실제 판단이 우선한다.

## 5. 검증 테스트 (software)

| 동작 | 테스트 |
|---|---|
| critical path·미측정 phase·B3·staging 대체·queue 선행 | `tests/test_jct_phases.py` |
| 기본 경로 불변, B3 선택과 native Job 연결, 품질·synthetic·runtime·route·workload·stale·memory 거절, fallback 기록, B4 busy/incomplete, 접수 시 drift 거절, freshness guard, B5 링크·데이터 위치, 데이터 접근 filter, pool manifest·Slurm feature·학습 guard·실제 노드 결합, API 권한·linkage | `tests/test_heterogeneous_recommendation.py` |
| 기존 동작 | `tests/test_jct_selection.py`, `tests/test_scheduling.py`, `tests/test_placement.py`, `tests/test_backends.py`, `tests/test_service.py`, `tests/test_api_flow.py` |
