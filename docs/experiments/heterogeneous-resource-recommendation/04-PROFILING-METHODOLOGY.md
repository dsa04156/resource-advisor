# 04. Profiling 방법

## 1. 단위와 경계

`JctProfile`은 한 workload/runtime/route identity에 대한 **native 경계 기반 phase 시간**이다. 모든 시간은 초, 같은 시계 도메인(native timestamp)이다.

| 구간 | 경계 | 원천 |
|---|---|---|
| queue wait | native 제출 → 할당 시작 (대기 중 선행 작업) | `RouteQueueSnapshot` + 접수된 RA intent로 예측 (profile 아님) |
| admission | native 제출 → 할당 시작 (빈 자원일 때) | `admission_seconds`; 선행 작업 뒤 재입장은 `readmission_seconds` |
| runtime_startup | 할당 시작 → 컨테이너/프로세스 시작 | Pod `startedAt` / Slurm start |
| data_staging | 별도 측정된 데이터 준비 | `staging_boundary=separate`일 때만 |
| model_preparation | 모델 load/compile/초기화 | workload가 별도 경계를 보고할 때만 |
| compute | workload가 측정한 계산 구간 | `ExecutionResult.measurements.elapsed_seconds` |
| communication | DDP/동기화 | 별도 경계가 있을 때만 |
| finalization | 결과 기록·정리 | 별도 경계가 있을 때만 |
| unattributed_in_container | 컨테이너 시간 − compute | 경계는 있으나 원인 미분해 |

규칙:

- 선언된 phase 중 하나라도 null이면 `PROFILE_COMPONENT_UNKNOWN` → `NEEDS_PROFILE`. 0으로 채우지 않는다.
- phase는 기본적으로 순차·비중첩이다. 겹치는 구간은 `overlap_groups`로 선언하고 그 그룹의 **최댓값(critical path)**만 더한다.
- warm service처럼 Job 이전에 준비된 구간은 `excluded_preexisting_phases`에 두며 같은 phase를 다시 셀 수 없다(계약 검증 오류).
- `staging_boundary=inside_compute`이면 staging이 compute 안에 있어 네트워크 대체가 불가능하다 → B5에서 `STAGING_NOT_SEPARABLE`.
- 불확실성은 `jct_stddev_seconds`(반복 간 표본 표준편차, 서술용)와 `repeats`만 보존한다. 보정된 신뢰구간·확률을 만들지 않는다.

## 2. 생성 경로

1. **RA 완료 Job에서 유도** ([`jct_profile_from_jobs`](../../../src/resource_advisor/recommendation.py)): `SUCCEEDED`, `quality_passed`, hardware evidence, `result_digest`가 저장된 결과와 일치, 같은 workload/context/candidate, **같은 실제 노드**(placement), 네 native 시각이 모두 있고 순서가 맞는 Job만. 하나라도 어긋나면 전체를 거절한다. 이 경로의 staging은 `inside_compute`다.

   ```bash
   rtk proxy uv run --locked python examples/hetero_collect.py profile \
     --database-url "$RA_DATABASE_URL" --job-id j-... --job-id j-... \
     --hardware-json hardware.json --ref jct-<route>-<date> --valid-for-seconds 86400 > profile.json
   ```

2. **operator import** (`source=operator_import`): 별도 phase 경계를 가진 workload의 측정값. 같은 계약 검증을 거친다.

등록은 operator 권한의 `POST /api/v1/compute/jct-profiles`이다. 불변이며 같은 ref 변경은 409, 미래 시각은 422.

## 3. 재사용 조건 (ranker 입장 시점)

identity(workload·context signature, runtime versions, environment digest), hardware fingerprint, route(backend·cluster·resource key·노드/pool member), workload 필드, 품질·synthetic, TTL, peak memory(`QualityPolicy.maximum_peak_memory_mib` 이하)를 모두 통과한 profile 중 가장 최근 것을 쓴다. 통과하지 못하면 가장 최근 profile의 거절 사유가 prediction에 남는다.

## 4. 실험에서의 분리

- profiling phase: route당 최대 1 Job(워밍업 겸용, 평가 결과에서 제외), legacy profile로 고정 후보 제출.
- profiling 결과로 만든 `JctProfile`을 등록한 뒤에 평가를 시작한다. 분석은 평가 첫 Job 생성 시각 이후에 측정된 profile을 쓴 결정을 `profile_after_cutoff`로 센다.
- 과거 기록 replay는 software 점검일 뿐 profile·실험 증거로 쓰지 않는다.
