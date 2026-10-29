# 07. 실험 계획 (사전 등록)

상태: **준비 단계 고정 계획**. 이 문서와 machine-readable 계획은 실행 전에 고정한다. 2026-10-29 software 단계에서는 Job을 제출하지 않았고 결과가 없다. 개선 여부를 미리 정하지 않으며 null/negative 결과도 보고한다.

## 1. 설계

- **Randomized complete block design.** 한 scenario·arrival pattern의 block마다 적용 가능한 arm(B0–B5)을 정확히 한 번씩, `random.Random("{seed}:{scenario}:{pattern}:{block}")`로 정한 순서로 실행한다. 같은 scenario는 같은 고정 arrival trace를 쓴다.
- **독립 단위**: (scenario, pattern, block, arm) cohort의 native Job. step·kernel 반복은 단위가 아니다.
- **carry-over 방지**: runner는 cohort의 모든 Job이 terminal이 되거나 runner가 취소할 때까지 다음 cohort를 시작하지 않는다. 외부 상태(다른 project 점유 등)는 reset하지 않고 cohort 직전에 `--capture-pods`로 기록한다.
- **반복**: block n=3. 예산 제약의 탐색적 목표이며 검정력·효과 크기·MDE·우월성을 주장하지 않는다.
- **profile/평가 분리**: profiling phase(route당 최대 1 Job, 워밍업 겸용, 평가에서 제외) → `JctProfile` 등록 → 평가. 평가 첫 Job 이후 측정된 profile을 쓴 결정은 `profile_after_cutoff`로 집계한다.
- **결과 지표**: 완료율(실패·timeout·취소 포함 분모), completed-only JCT(native 제출→native 완료), queue wait(native 제출→할당 시작), censored 수, abstain(`NEEDS_PROFILE`/`ESTIMATE_UNAVAILABLE` 거절) 수, fallback 수, profiling 비용.

## 2. 예산 (hard)

| 항목 | 상한 |
|---|---|
| owned native Job | ≤60 (profiling 포함) |
| 실험 wall | ≤1,800초 |
| 가속기 slot-초 | ≤1,800 |
| profiling Job | route당 ≤1 |
| Job당 시간 | ≤120초, 초과 시 runner가 **자기 Job만** 취소(censored) |
| 반복 | block 3 |

계획 단계에서 `expected_job_seconds`로 예상 Job·slot-초·wall을 계산해 우선순위(E6, E5, E1, E2, E3, E4) 순으로 들어가는 scenario만 PLANNED, 넘으면 `DEFERRED_BUDGET`. 실행 중에도 누적 Job·관측 slot-초·wall을 확인해 다음 cohort가 넘으면 `NOT_RUN_BUDGET`으로 기록하고 멈춘다.

## 3. 고정된 준비 계획 (2026-10-29)

- 입력: [plan/capacity-20261029.json](plan/capacity-20261029.json) (SHA256 `0529b8bc9414a31950fbbe5d6a646c9b744f9a99891543a3398f593cfd82e72d`) — 감사 §4 읽기 전용 관측의 사실값.
- 계획: [plan/plan-20261029-preparation.json](plan/plan-20261029-preparation.json), seed `20261029`, 계획 digest `sha256:b291f44e111a8fa7184cf8d8163c95efda4c56b2527e9f220d163e3515c44a3d`, 파일 SHA256 `d02c246505cd4bf10691f6e6f5ecd1b6f23047f4397efb9632396af749cf6e5e`.
- 상태표: [plan/scenario-status-20261029.csv](plan/scenario-status-20261029.csv)

| 실험 | 상태 | 이유 (오늘 관측) |
|---|---|---|
| E1 단일 GPU 학습 | BLOCKED | 자유 GPU 학습 route 0 (exclusive GPU 3대 다른 project 점유, Orin shared slot 학습 variant 없음) |
| E2 sparse/moderate/burst | BLOCKED | GPU 학습 route <2 |
| E3 같은 데이터 local/remote | BLOCKED | 측정 링크·DataLocation 0 |
| E4 DDP/NCCL | NOT_VALIDATED | exclusive GPU <2, NCCL route 없음 |
| E5 동등 추론 | BLOCKED | NPU resource는 있으나 오늘 runtime 실행 미검증, 서로 다른 compiled precision은 한 WorkloadSpec이 아님 |
| E6 Slurm 공통 결과 | BLOCKED | Slurm VM·K8s CPU 학습 route를 오늘 재검증하지 않음(2026-10-28 기록은 과거) |
| E7 신뢰성 | PLANNED (Job 0) | software fault injection: stale·runtime·queue·network·mismatch 거절은 `tests/test_heterogeneous_recommendation.py`에서 확인. 실장비 부분은 E5/E6 route가 생기면 같은 preview로 반복 |
| E8 profiling ROI | BLOCKED | 원천 실험(E1/E2/E5/E6) 없음 |

**따라서 오늘 계획의 평가 Job은 0개다.** 이것은 결과가 아니라 장비 상태에 따른 사전 판단이다.

## 4. 다음 실장비 dispatch 절차

1. 읽기 전용 재관측: `hetero_collect.py inventory --kubectl --local`, Slurm `sinfo`, 점유 Pod(`queue --kubectl`).
2. 실행 가능한 route를 실제로 등록·검증한 뒤(기존 qualification·training isolation 절차, 승인 필요 변경은 소유자 승인 후) [plan/capacity-template.json](plan/capacity-template.json)에 사실값과 binding(workload ref, 고정 후보, RR 순서, profiling route, arm별 scheduling profile ref)을 채운다. 사실값을 추정해 넣지 않는다.
3. 고정과 확인:
   ```bash
   rtk proxy uv run --locked python examples/hetero_experiment.py freeze --capacity <fresh.json> --seed <seed> --out <plan.json>
   rtk proxy uv run --locked python examples/hetero_experiment.py check --plan <plan.json>
   rtk proxy sha256sum <plan.json>   # 이 문서에 digest·SHA256을 기록한 뒤 실행
   ```
4. dry-run → 실행:
   ```bash
   rtk proxy uv run --locked python examples/hetero_experiment.py run --plan <plan.json> --digest <digest> --profiling-profile <legacy-profile>
   RA_TOKEN=... rtk proxy uv run --locked python examples/hetero_experiment.py run --plan <plan.json> --digest <digest> \
     --profiling-profile <legacy-profile> --execute --api <url> --capture-pods > raw.jsonl
   ```
   `--profiling-profile`이 있으면 profiling phase만 실행하고 끝난다. 그 결과로 `hetero_collect.py profile`을 만들어 `POST /jct-profiles`로 등록한 다음, `--profiling-profile` 없이 같은 plan으로 `run --execute`를 호출해 평가를 실행한다(Idempotency-Key가 plan digest+unit에 묶여 같은 unit이 중복 제출되지 않음).
5. 분석: `analyze --plan <plan.json> --raw raw.jsonl --out <dir>` → `results.csv`, `scenario-status.csv`, `evidence-manifest.json`. replay·dry-run 기록은 거절된다. 그림은 이 live 기록으로만 만든다.

## 5. 지원하지 않는 경우의 재실행 명령

`check` 출력의 `rerun` 항목이 각 BLOCKED/NOT_VALIDATED 실험에 대해 "재관측 → 재고정 → `--only <E>` 실행" 명령을 준다. 오늘 계획에서는 E1–E6, E8이 해당한다.
