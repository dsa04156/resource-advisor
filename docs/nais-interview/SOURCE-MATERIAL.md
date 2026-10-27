# 분산된 연구자원을, 하나의 실행 흐름으로

**NAIS의 공동활용 목표와 Kubernetes·Slurm 기반 연산 관리 구현**

NAIS 연구기술직-3(플랫폼, GPU·이기종 컴퓨팅 운영) 면접 발표용 원재료. 사용자가 PPT로 직접
배치한다. 작성 2026-10-27. 증거는 2026년 9–10월의 개별 실험 기록이며 **현재 live 상태가 아니다.** 날짜가 없는 원본은 날짜 미상으로 표시한다.

## 읽는 법: 네 가지 표시

| 표시 | 뜻 | 근거 |
|---|---|---|
| **[공식 방향]** | NST/정부가 밝힌 목표·계획. 이미 가동 중인 구조나 달성된 로드맵이 아니다. | 공식 URL |
| **[설계 해석]** | 공식 방향을 이 프로젝트가 어떻게 받아들였는지에 대한 본인의 해석. | 이 문서 |
| **[실습 구현]** | 소규모 lab에서 실제로 작성·실행한 코드와 증거. | 로컬 소스·evidence |
| **[미구현]** | 기관 연합 규모에서 필요하지만 이 프로젝트에 없는 범위. | `architecture.md` 한계 절 |

슬라이드 하나에 표시 하나를 붙이면 청중이 “계획”과 “구현”을 혼동하지 않는다.

---

## 1. NAIS가 하려는 것 [공식 방향]

### 1.1 핵심 목표 (공식 자료 요약)

| 축 | 공식 자료에서 확인한 방향 | 출처 |
|---|---|---|
| 연구 가속 | AI와 과학기술 연구를 결합해 연구 생산성을 높인다 | [NST NAIS 소개][nst] |
| 공동 기반 | 연구 데이터·연산자원·모델·전문지식을 연결하는 연구 지원 기반을 마련한다 | [NST NAIS 소개][nst] |
| 연구 방식 | 자율적으로 연구를 수행하는 AI와 실험실 자동화, 융합 연구를 추진한다 | [NST NAIS 소개][nst] |
| 연구자 접근 | 기관별로 흩어진 슈퍼컴퓨터·GPU 등을 연구자가 찾아 신청하고 활용하도록 연결한다 | [KTV, 2026-05-06][ktv] |
| 연구자산 재사용 | 연산과 공통 모델/API를 지원하고, 사업에서 만든 AI-ready 연구자산을 AI-OS에 축적한다 | [NST Seed형 지원 보도자료][seed] |

따라서 NAIS의 목표는 GPU 배분만으로 설명되지 않는다. 이 레포가 다루는 부분은 그중
**공동 연산자원을 사용하는 실행·검증·운영 계층**이다.

### 1.2 Hub와 Spoke의 책임 [공식 자료 요약]

| NAIS (Hub) | 출연연 (Spoke) |
|---|---|
| 여러 기관에서 재사용할 데이터 규칙·기반 모델·평가 기준 마련 | 분야별 연구 데이터를 확보 |
| **공통 GPU 자원의 배치 체계를 연결** | 분야에 맞게 모델 조정 |
| 수행 후 성과를 평가하는 체계 | **자기 기관의 연구 우선순위를 자율 결정** |
| | 분야에 맞는 성과 지표 결정 |

발표 포인트: 공동 풀을 만들되 **기관의 우선순위 자율성은 유지**한다는 두 조건이 함께 있다.
이 두 조건이 2장의 설계(공통 계약 + native scheduler 권한 유지)로 이어진다.

### 1.3 로드맵 [공식 방향 — 계획]

| 기간 | 공식 계획 |
|---|---|
| ’26 기반 구축 | 연구자 접속 포털과 AI-OS 시험판 마련, **GPU 공유·현황 조회 시작을 목표** |
| ’27~’28 서비스 본격화 | AI-OS 서비스를 정식 제공하고 연산 기반을 순차 확대 |
| ’29~’31 국가 인프라 정착 | 확보한 기반의 운영을 정착시키고 외부 참여를 확대 |

출처: [NST NAIS 소개][nst]. 계획이며 달성 여부를 이 문서가 주장하지 않는다.

### 1.4 연구기술직-3 직무가 요구하는 것 [공식 방향]

출처: [공식 직무기술서 ZIP][zip] 안 `…(연구기술직-3 플랫폼).pdf` (모집 분야 “GPU·이기종 컴퓨팅 운영”, 2명).

아래는 직무기술서를 요약한 것이며, 원문 전체는 링크한 PDF에서 확인한다.

- NVIDIA·AMD·국산 NPU 등 서로 다른 가속기의 설치·운영·장애 대응과 공동 활용.
- 작업 배치와 할당 한도·우선순위 관리, 이용 현황 관측, 사용량에 근거한 배분.
- 슈퍼컴퓨터와 연결하고 자체 장비 및 클라우드 연산자원을 함께 운영.
- 컨테이너·스토리지·네트워크 및 공급자별 드라이버·런타임·컴파일러 검증.
- 데이터 복원, 접근 통제, 감사 기록 관리.
- Slurm/Kubernetes, Prometheus/Grafana, 배포 자동화와 고속 네트워크·병렬 스토리지 관련 역량.

---

## 2. 공식 목표 ↔ 설계 해석 ↔ 실습 구현 (짝 맞춤 표)

PPT의 중심 슬라이드 후보. 왼쪽은 공식, 가운데는 해석, 오른쪽은 실제 코드.

| [공식 방향] | [설계 해석] | [실습 구현] (근거 파일) |
|---|---|---|
| 연구자가 자원을 찾아 신청 | 연구자는 backend가 아니라 **하나의 workload 계약**으로 요청해야 한다 | `WorkloadSpec`·`JobRequest`·`RuntimeVariant` 계약 ([contracts.py][contracts]) |
| GPU 풀·스케줄링 통합 (Hub) | 통합은 “스케줄러 교체”가 아니라 **공통 제출·검증 계층** | project×cluster route → Kubernetes/Slurm adapter ([configuration.py][config], [backends.py][backends]) |
| 우선순위 설정은 출연연 자율 (Spoke) | 배치·우선순위 결정권은 **각 native scheduler**에 남긴다 | 공통 등급 normal/high → Kueue priority class / Slurm QOS 매핑만 수행 ([backends.py][backends] `priority_mapping`, [slurm-priority-gateway.md][gw]) |
| 이기종 가속기 SW 스택 검증 | 실행 환경이 **검증된 것과 같은지** 매번 확인 | Slurm native runtime manifest SHA-256 검증 ([runtime_guard.py][guard]) |
| 통합 현황판·사용량 기반 배분 | 사용량은 **결과·종료 상태와 같은 트랜잭션**으로 남겨야 회계가 맞다 | 종료 상태 + usage ledger 원자적 기록, S3/API/MLflow publication ([architecture.md][arch], [worker.py][worker]) |
| 수행 후 성과를 평가하는 체계 | 프로세스 성공 ≠ 결과 성공 | `COLLECTING` 후 digest·attempt epoch·signature·work unit·quality 검사 ([service.py][service] `ingest`) |

**범위 문장(그대로 사용 가능):** 이 프로젝트는 NAIS가 말하는 공동활용 체계 중 *공통 실행·검증
계층*을 소규모 lab에서 구현한 것이다. 기관 간 연합(federation) 완성이 아니며, 각 클러스터의
native scheduler 정책이 여전히 최종 권한을 가진다.

---

## 3. 실제 아키텍처 [실습 구현 + 기존 도구]

### 3.1 구성 요소: 기존 도구 vs 프로젝트 구현

| 계층 | 기존 도구 (사용만 함) | 프로젝트 구현 (코드로 확인) |
|---|---|---|
| 요청 | — | REST API: project 범위 인증, 멱등 키(request digest), 불변 workload/variant 참조 ([service.py][service]) |
| 상태 원장 | PostgreSQL | Job 상태기계·제출 intent·usage ledger 스키마와 전이 규칙 ([contracts.py][contracts] `State`, [worker.py][worker]) |
| 라우팅 | — | `routes: [{project, cluster, backend, options}]` → backend 인스턴스, (project, cluster) 중복 금지 ([configuration.py][config]) |
| Kubernetes 경로 | Kubernetes Job, **Kueue** admission, kube-scheduler 배치 | `KubernetesBackend`: `suspend: true` Job + `kueue.x-k8s.io/queue-name`/`priority-class` 라벨 생성, 상태 관측 ([backends.py][backends]); container 이미지는 `@sha256:` digest 고정 필수 ([contracts.py][contracts] `RuntimeVariant`) |
| Slurm 경로 | Slurm `sbatch`/`squeue`/`sacct`/`scancel`, 제한된 SSH gateway | `SlurmBackend`: 안전 식별자 검증(partition/account/QOS), SSH alias 실행(비밀번호 없음), `sbatch --parsable` ID 확인, `sacct` 실패를 “없음”이 아닌 UNKNOWN으로 처리, **container variant 거절** ([backends.py][backends]) |
| 런타임 검증 | — | native runtime guard: manifest digest, environment digest, architecture, 허용 command, 실행 파일 hash ([runtime_guard.py][guard]) |
| 결과 수집 | — | `COLLECTING` 상태에서 결과 envelope 검증 후 `SUCCEEDED`/`RESULT_INVALID` ([worker.py][worker], [service.py][service]) |
| 게시 | S3 호환 저장소, MLflow | 결과 bytes 조건부 저장, MLflow run/artifact 전달(재시도형, exactly-once 아님) ([worker.py][worker] `MLflowDelivery`) |

### 3.2 흐름도 텍스트 (도식으로 옮길 것)

```
연구자 / 파이프라인
   │  POST job (project 토큰, 멱등 키)
   ▼
[구현] REST API ── 검증: project 범위, 불변 workload·variant, capability snapshot TTL
   │
   ▼
[기존] PostgreSQL  ◀──▶  [구현] Worker (project × cluster route)
   │                          │
   │          ┌───────────────┴────────────────┐
   │          ▼                                ▼
   │  [구현] Kubernetes adapter        [구현] Slurm adapter
   │          │ suspend:true Job               │ 제한 SSH → sbatch
   │          ▼                                ▼
   │  [기존] Kueue admission            [기존] Slurm 큐/QOS
   │  [기존] kube-scheduler 배치        [구현] native runtime guard(hash)
   │          └───────────────┬────────────────┘
   │                          ▼
   │                 [구현] COLLECTING → 결과 검사
   │                 (digest·epoch·signature·work units·quality)
   │                          ▼
   └──────────▶ [구현/기존] 종료 상태+usage ledger(Postgres) · S3 · MLflow
```

**도식에 반드시 넣을 경계:** 두 backend는 **서로 다른 장비 풀**이다. 같은 장비를 K8s와 Slurm이
공동 스케줄링하지 않는다. 배치 결정은 각 native scheduler가 한다.

### 3.3 Job 상태기계 (contracts.py `State`)

`RECEIVED → VALIDATED → SUBMITTING → QUEUED → RUNNING → COLLECTING → SUCCEEDED`

분기: `SUBMISSION_UNKNOWN`, `FAILED`, `RESULT_INVALID`, `CANCEL_REQUESTED → CANCELED`

| 단계 | 일어나는 일 | 코드 |
|---|---|---|
| VALIDATED | 요청 검증, 사전 backend 검증 실패는 원격 호출 전이므로 바로 `FAILED(PRE_SUBMISSION_BACKEND)` | [worker.py][worker] `submit_one` |
| SUBMITTING | **원격 호출 전에 intent를 DB에 커밋**. 이후 crash는 reconcile 대상 | [worker.py][worker] “Commit intent before the remote call” |
| SUBMISSION_UNKNOWN | 응답 유실·비정상 응답 → 같은 attempt의 외부 ID를 탐색(reconcile). 확정 불가하면 운영자 판단. **자동 재제출 없음** | [worker.py][worker], [architecture.md][arch] |
| QUEUED | K8s: suspended Job이 Kueue admission 대기 / Slurm: PENDING. `max_queue_seconds` 초과 시 취소 요청 | [worker.py][worker] `_reconcile_observation` |
| RUNNING | native scheduler가 배치. Slurm은 guard가 runtime hash 확인 후 실행 | [backends.py][backends], [runtime_guard.py][guard] |
| COLLECTING | backend 완료 ≠ 성공. 결과 envelope 검증, 수집 기한 초과 시 `RESULT_INVALID(RESULT_COLLECTION_DEADLINE)` | [worker.py][worker], [service.py][service] |
| 종료 | 종료 상태와 ledger 기록 원자적. 실패·취소·사전 거절도 ledger에 남김 | [architecture.md][arch] |

---

## 4. 프로젝트에서 구현한 책임 영역 [실습 구현]

| # | 영역 | 무엇을 막는가 | 근거 |
|---|---|---|---|
| 1 | 공통 workload/runtime 계약 | backend별 요청 형식이 연구자에게 새어 나가는 것. workload signature(모델·데이터·정밀도·work unit·품질 계약)와 context signature(하드웨어·runtime·자원) 분리 | [contracts.py][contracts], [architecture.md][arch] “Why two signatures?” |
| 2 | project route와 adapter | 한 project가 허용되지 않은 cluster/backend로 제출되는 것 | [configuration.py][config], [backends.py][backends] |
| 3 | 원격 호출 전 제출 intent | crash 시 “제출했는지 모르는” 작업이 원장 밖에 남는 것 | [worker.py][worker] |
| 4 | SUBMISSION_UNKNOWN과 reconcile, 맹목 재시도 금지 | 응답만 유실된 작업을 다시 제출해 **유료 workload가 두 번** 도는 것 | [worker.py][worker], [backends.py][backends] `SubmissionUnknown` |
| 5 | 결과 identity·hash·work unit·quality 검사 | 다른 attempt의 결과, 변조·불완전 결과, 품질 미달 결과가 추천 profile로 들어가는 것 | [service.py][service] `ingest` |
| 6 | 결과·사용량 publication | 사용량과 결과가 따로 기록되어 회계가 틀어지는 것 | [worker.py][worker] `MLflowDelivery`, [architecture.md][arch] |

**말하지 않을 것:** 위 복구 경로가 모두 live로 검증됐다는 주장. 일부(accepted-submit crash 복구,
두 worker 동시 실행, PostgreSQL 복원 등)만 bounded live 증거가 있고, Slurm 복구·만료 lease
fencing·E0–E7 전체 시나리오는 미검증이다 ([architecture.md][arch] “Current operational limits”).

---

## 5. 구체적 증거

아래 5.1–5.3은 **2026-10-08 기록**이다. 뒤의 운영 증거는 개별 시점·조건이 다르다. 표본을 합쳐 하나의 성능 실험처럼 해석하지 않는다.

### 5.1 Slurm 폐루프: 관측 → 추천 → 승인 실행 → 피드백 (주 증거)

실험 ID `right-sizing-slurm-20261008-v1`. 단일 고정 Slurm 후보(native Orin), GPU1/CPU1/메모리1GiB,
native 120초 한도. 출처: [right-sizing-slurm-feedback.md][slurmdoc], [attempts CSV][slurmcsv],
[audit JSON][slurmaudit] (사본: [evidence/slurm-closed-loop-20261008.csv](evidence/slurm-closed-loop-20261008.csv), job/attempt ID 열 제거).

| 단계 | Native Job | 결과 | 측정 순전파 합(ms) | GPU 예약초 |
|---|---:|---|---:|---:|
| 원본 qualification 관측 | 77 | SUCCEEDED | 14.933 | 89 |
| 새 workload 관측 1 | 78 | SUCCEEDED | 13.306 | 85 |
| 관측 2 | 79 | SUCCEEDED | 11.264 | 83 |
| 관측 3 | 80 | SUCCEEDED | 11.639 | 84 |
| 추천 승인 후 독립 실행 | 81 | SUCCEEDED / feedback COMPARABLE | 11.987 | 80 |
| **합계** | **5개** | **5/5 성공** | | **421** |

표의 compute 값은 각 실행에서 측정한 순전파 구간의 합이며 단일 호출 지연이나 전체 Job 실행시간이 아니다.

추가 수치 (audit JSON):

| 항목 | 값 | 단위·조건 |
|---|---|---|
| GPU 예약 | 421 | 초, startup·full verification 포함 |
| CPU core 예약 | 421 | core·초 |
| Host memory | 431,104 | MiB·초 |
| Protocol wall | 549.050608 | 초 |
| Reference 순전파 평균 | 12.069607 | ms, 관측 3회 기준 |
| Descriptive 평균 구간 | 10.187056–13.952157 | ms, calibrated 예측구간 아님 |
| 승인 실행 순전파 | 11.986714 | ms |
| Residual | −0.686787 | % |
| 다음 추천 history source | 4 | 개 (승인 실행 결과가 포함됨) |

흐름 의미: history 없을 때 `NEEDS_PROFILE`로 추천을 보류 → 실제 결과 3개로 lookup 추천 →
명시적 API 승인 → 독립 실행 → `VERIFIED` 피드백 저장. 각 attempt는 usage 1개·MLflow run 1개,
S3/API/MLflow bytes 일치. 회귀 검사 14개가 정상 재계산과 오류 거절을 확인 ([slurmdoc]).

**한계 (슬라이드 하단 각주용):** 단일 구성이므로 자원 선택의 성능 개선이 아니다. Slurm active
search, backend 간 속도 비교, DDP가 아니다. 에너지·실제 model CPU time·환경 준비 비용은
unknown. 추천 TTL(15분) 경과 후 readback은 `NEEDS_RECONFIRMATION` — 과거 승인을 자동 부활시키지
않는다 ([pool-resume-readback-v1.json][resume]).

### 5.2 Kubernetes 5노드 고정 workload (보조 증거, 별도 표본)

출처: [profile-pool-20261008-v2-results.md][pool], [main.csv][poolcsv], [summary.json][poolsum].

| 항목 | 값 |
|---|---|
| 모델 | 학습된 FP32 MLP 64→256→256→10 (손글씨 숫자) |
| Held-out 정확도 | 98.046875% (고정 이미지 256개) |
| 노드 | Kubernetes GPU 노드 5대: RTX5060Ti, RTX5080, GB10, Orin Nano, AGX Orin |
| 큐 | 기존 Kueue `ra-f0-gpu`, normal priority, CPU1·memory512Mi·accelerator slot1 |
| 작업량 | 작업당 16,384 forwards × 256 images = 4,194,304 반복 추론 |
| 본 실행 | **10/10 성공** (Baseline 5 + Profile reuse 5), 10개 모두 quality 1.0 |
| 전체 | 16개 중 15 성공·1 실패 (GB10 RuntimeClass 누락, 기록 보존) |

이 실험은 기존 CUDA 실행 경로를 재사용한 **실험용 dispatcher**로 돌렸다. 운영 API·SQL·MLflow
통합 경로의 검증이 아니며, Slurm Job은 0개다(당시 컨트롤러 접속 불가). 5.1과 **다른 표본**이다.

정책 비교 수치를 쓸 경우 반드시 같이 쓴다 (n=1/조건):

| 지표 | Baseline | Profile reuse | 변화 |
|---|---:|---:|---:|
| 동기화 추론 구간 합 | 14.967436초 | 4.153377초 | −72.25% |
| 본 실행 accelerator-slot 할당 | 25초 | 10초 | −60% |
| 평균 JCT | 6.0초 | 6.2초 | +3.33% |
| 처리량 | 1,636.36 jobs/h | 1,384.62 jobs/h | −15.38% |
| 물리 GPU-hours / 연속 GPU 사용률 | unknown | unknown | — |

해석: 빠른 GPU에 몰아도 같은 GPU 후속 작업의 재입장 대기가 연산 절감을 상쇄했다. 슬롯 시간
감소를 **물리 GPU 비용 절감으로 쓰지 않는다** (공유 슬롯 포함, 1초 해상도).

### 5.3 부록: 162작업 JCT 정책 비교 (검증 중단, 개선 없음)

출처: [jct-profile-queue-20261008-short-v1-recorded-results.md][jct]. 원시 기록은
[보존된 작업별 CSV](../evidence/jct-profile-queue-20261008-short-v1-recorded/recorded-main.csv), [회차별 CSV](../evidence/jct-profile-queue-20261008-short-v1-recorded/recorded-cohorts.csv), [감사 중단 기록](../evidence/jct-profile-queue-20261008-short-v1-recorded/verification-stopped.json)에서 확인한다.

- 후보 6개(K8s 5 + Slurm Orin Nano 1), 부하 3종(sparse 3초/moderate 0.5초/burst 0초) × 정책 3종 × 3회, 회차당 6작업 = **162작업, 기록상 162 SUCCEEDED** (backend 별 K8s 143, Slurm 19 — `recorded-main.csv` 집계).
- **최종 독립 감사·통합 게이트 판정은 사용자 요청으로 중단**됐다. 실제 스케줄링 경로에 반영하지 않았다.

| 정책 | 평균 JCT(s) | p95 JCT(s) | 처리량(job/s) |
|---|---:|---:|---:|
| Round Robin | 6.537 | 11.000 | 0.4000 |
| Profile Only | 6.389 | 17.000 | 0.3529 |
| Profile + Queue | 6.556 | 14.000 | 0.3699 |

**전체 평균 JCT 개선은 관측되지 않았다.** Profile + Queue 54건 중 6건은 큐 상태 불확실로
Round Robin fallback. 쓸 수 있는 메시지: “개선이 없으면 반영하지 않는 판단과 원시 기록 보존.”

### 5.4 Slurm 정책 집행: 자원 한도와 우선순위

2026-10-02, Slurm 24.11.5, GPU 한 대를 둔 별도 lab에서 실행했다.
[설명](../reference/slurm-policy.md) ·
[원본 JSON](../evidence/slurm-policy.json).

| 입력·설정 | 관측 결과 |
|---|---|
| 계정 전체 한도 CPU1·1GiB·GPU1, 작업별 최대 2분 | 한도를 적용하는 account/QOS 정책 구성 |
| CPU2 요청 | QOSMaxCpuPerJobLimit 거절 |
| 메모리2GiB 요청 | QOSMaxMemoryPerJob 거절 |
| 3분 요청 | QOSMaxWallDurationPerJobLimit 거절 |
| 허가되지 않은 QOS 요청 | Invalid qos specification 거절 |
| blocker 실행 중 normal/high 제출 | 둘 다 AssocGrpCpuLimit으로 대기 |
| normal을 먼저 제출한 뒤 blocker 해제 | high 05:48:38 UTC 시작, normal 05:48:41 시작 |
| QOS 우선순위 10/100 | 실제 작업 priority 100/1000 |

이것은 **한도 거절과 대기 작업 순서** 증거다. 선점, 다중 사용자 공정성, 물리 CPU 포화 증거가 아니다.
별도의 [API high 실행](../reference/slurm-priority-gateway.md)은
공통 priority → gateway → Slurm QOS 매핑과 결과 저장을 확인했으며 예약량은 72 GPU·초다.

### 5.5 Slurm API 실패 원인과 수정

2026-10-06 [설명](../reference/slurm-api-results.md) ·
[원본 JSON](../evidence/slurm-api-v1.json).

- 첫 실행: 전용 executor가 지정 DRM render 장치를 읽고 쓸 권한이 없어 CUDA 실행 실패. GPU 예약 74초.
- controller와 worker의 render 그룹 숫자 ID 차이를 확인하고, 대상 사용자·장치에 한정한 ACL 및 udev 규칙으로 수정.
- 재실행: 성공, GPU 예약 72초. 실패 포함 합계 146초로 기록.
- FP32 CNN의 수치 비교 400개를 통과. 순전파 10회 합계 11.012711ms, 중앙값 1.075626ms.
- PyTorch tensor 최고 할당 33.056152MiB. 이 값은 전체 보드 메모리나 프로세스 RSS가 아니다.
- runtime manifest 파일 항목 26,649개를 검증. 환경 준비·검증도 예약 구간을 소비하므로 72초와 11ms는 다른 지표다.
- API/S3/MLflow 결과 bytes 일치와 usage 1개·MLflow run 1개 확인.

단일 수정 사례이며 CUDA 전반의 안정성 또는 학습 모델 정확도 검증이 아니다.

### 5.6 PostgreSQL 복원과 서비스 재시작

[DB 설명](../reference/persistent-postgres.md) ·
[DB 원본](../evidence/persistent-postgres.json) ·
[서비스 설명](../reference/service-deployment.md) ·
[서비스 원본](../evidence/service-deployment.json).
두 원본 JSON에는 실험 날짜 필드가 없어 **정확한 실행일 미상**으로 둔다.

| 대상 | 확인한 수치·행동 |
|---|---|
| DB 복사 | entities 655, jobs 52, outbox 201, studies 8, usage 52행: 5개 테이블 canonical SHA-256 일치 |
| Pod 교체 | 새 Pod UID, 같은 PVC UID에서 테이블 지문 유지 |
| 외부 참조 | 기존 artifact 46개 bytes/metadata 일치, 기존 MLflow 링크 52개 접근 확인 |
| 서비스 재시작 | API Pod 교체 + collector의 처리된 SIGTERM 종료 후 자동 재시작 |
| 복구 관측 | 1회 combined recovery 6.78초, 과거 job 52개 재조회 |
| 권한 | 앱 DB 계정의 DB/role 생성 거절, inventory의 Secret 읽기·Job 생성 거절 |

기존 object를 읽어 대조했으므로 **object store 복원 증거는 아니다**. 같은 노드 PVC의 Pod 교체는
노드·디스크 소실 복구나 HA/PITR을 입증하지 않는다. 6.78초는 SLO/RTO가 아니며,
처리된 SIGTERM을 비정상 강제 종료 복구로 표현하지 않는다.

### 5.7 Hailo NPU: 이기종 실행과 품질 계약의 별도 증거

[ResNet-50 설명](../reference/hailo-resnet50.md) ·
[플랫폼 원본](../evidence/hailo-resnet50-platform.json) ·
[자격 검증 원본](../evidence/hailo-resnet50-qualification.json).
도우미가 실행한 실험임이 원문에 명시돼 있다.

| 항목 | 관측 |
|---|---|
| 고정된 Imagenette 100장 | NPU top-1 80/100, TFLite 기준 79/100, 양쪽 예측 일치 97/100 |
| 사전 qualification | 100회 동기 호출 합 0.339087234초, NPU 예약 8초 |
| API 관측 3회 | 호출 합 0.339502249 / 0.339822885 / 0.339404005초 |
| 승인 후 실행 | 0.343546645초, 같은 80% 정확도·97% 일치 |
| 사용량 | qualification 1건+API 4건=총5건, 20 NPU 예약초. API ledger에는 12초 |
| 결과 경로 | API → Kueue → KubeEdge → 결과 검사 → PostgreSQL/S3/MLflow |

같은 100장을 반복했으므로 독립 정확도 표본 400장이 아니다. 승인 실행 시간은 세 관측의
평균 구간 밖에 있어 예측구간 보장도 아니다. Hailo-8의 특정 HEF·입력·런타임만 검증됐으며
AMD GPU나 국산 NPU 전체로 일반화할 수 없다. NPU 메모리·전력·이용률은 미측정이다.
이전 ResNet18/EfficientFormer 품질 실패는 삭제하거나 성공으로 바꾸지 않았다.

### 5.8 데이터의 단위와 합산 규칙

- GPU 예약초: 배정 GPU 수 × 배정 기간. GPU 실제 이용률·연산 시간·전력과 별개.
- accelerator-slot 초: 공유 슬롯 수 × 배정 기간. 물리 GPU·초와 일괄 변환 불가.
- JCT: 실험별 원본의 시작·종료 정의를 확인. 순전파 지연과 같지 않음.
- quality 1.0: 해당 계약의 검사 통과 의미일 수 있음. 분류 정확도 100%로 바꾸지 않음.
- 동일 이미지 반복 추론: 호출 수는 커져도 독립 데이터 수는 늘지 않음.
- unknown/null: 측정 안 됨. 0으로 취급하지 않음.
- 서로 다른 실험의 성공/실패·준비·qualification 비용을 임의로 합쳐 플랫폼 전체 성과로 쓰지 않음.

### 5.9 쓰지 않을 주장

- GPU 사용률 개선, 물리 GPU 비용·에너지 절감
- BO/qLogNEI 우월성 (동일 예산 비교에서 선택 이점 없음: [policy-comparison-v2.md][policy])
- K8s와 Slurm이 같은 장비를 함께 스케줄링
- 모든 장애·복구 경우의 live 검증
- 현재 시점 live 운영 상태, production readiness, 기관 연합 완성

---

## 6. 범위: 구현된 lab 경로 vs 미구현

| [실습 구현] lab 경로 | [미구현] 기관 규모에 필요한 것 |
|---|---|
| project 토큰 기반 범위 인증 | 기관 간 identity 연합(SSO·계정 매핑) |
| lab 내부 네트워크, 제한 SSH gateway | 기관 간 네트워크·방화벽·전용망 설계 |
| scoped credential, read-only runtime mount | Pod security·장치 격리 인증, 감사 체계 |
| Kueue quota·Slurm QOS 매핑 | 기관 간 **자원 배분 협약**·공정 분배 정책 |
| NVIDIA GPU, Jetson, Hailo NPU 일부 | **AMD GPU(ROCm) 검증**, 국산 NPU 범용 검증 |
| 자체 보유 노드 | **클라우드 GPU** 연동·검증 |
| Slurm native runtime | Slurm container executor (현재 container variant는 preflight 거절) |
| 일부 복구 경로 bounded live | Slurm 복구, lease fencing, 단절 노드 회계, E0–E7 전체 |

근거: [architecture.md][arch] “Current operational limits”, [slurm-runtime.md][slurmrt].

### 6.1 연구기술직-3 직무와의 연결

| 직무기술서 항목 [공식] | 이 프로젝트에서 보여줄 수 있는 것 [실습] |
|---|---|
| 자원 스케줄링·쿼터·우선순위 정책 운영 | Kueue admission(suspended Job)·Slurm QOS로 공통 normal/high 매핑, 결정권은 native |
| 이기종 가속기 SW 스택 검증 | runtime manifest hash 검증, 결과 quality·work unit 검사 |
| 가동률 모니터링·통합 현황판·사용량 기반 배분 | 종료 상태와 usage ledger 원자 기록, 예약초 단위 분리(GPU/CPU/memory) |
| 백업·복구, 접근 권한·감사 기록 | 제출 intent·reconcile, 불변 결과, PostgreSQL 복원 bounded 증거 |
| 슈퍼컴퓨터 연계·통합 | Slurm adapter를 통한 HPC 스케줄러 연계 (lab 규모) |

---

## 7. Run:ai와의 차이 — 제품의 우열보다 맡는 계층

| 비교 축 | NVIDIA Run:ai 공식 기능 | 이 레포의 구현·경계 |
|---|---|---|
| 주된 역할 | Kubernetes 기반 워크로드의 자원 배분·운영 | 공통 요청 계약을 서로 다른 K8s/Slurm 실행 경로와 연결하고 결과·사용량을 검증 |
| 스케줄링 | quota, fair share, priority, preemption, topology 관련 정책 | Kueue와 Slurm의 기존 배치 기능 사용. 자체 커널 스케줄러 구현 아님 |
| GPU 분할 | GPU fractions 및 관련 공유 모드 제공 | 동등한 GPU 분할 기술을 새로 구현하지 않음. 실험의 accelerator slot과 물리 GPU를 구분 |
| Slurm 접점 | Kubernetes에서 Slurm을 구성하는 예제와 Slinky NodeSet 지원 문서가 있음 | 별도 Slurm 컨트롤러에 제한 SSH로 sbatch/squeue/sacct/scancel 수행 |
| 통합 범위 | NodeSet 지원이 Slurm job/account/partition 관리까지 의미하지는 않음 | project×cluster route로 별도 backend 연결. 기관 간 계정·네트워크 연합은 미완성 |
| 결과 검증 | 이번 조사에서 이 프로젝트와 같은 결과 계약을 비교 시험하지 않음 | attempt·signature·hash·품질·작업량 검사, 결과/원장/MLflow 추적 |

공식 근거: [Run:ai 제품 소개](https://www.nvidia.com/en-us/software/run-ai/),
[스케줄러 동작](https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/platform-management/runai-scheduler/scheduling/how-the-scheduler-works),
[GPU 공유 모드](https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/platform-management/runai-scheduler/resource-optimization/gpu-fractions/compute-modes),
[지원 workload와 NodeSet 범위](https://docs.nvidia.com/run-ai/self-hosted/documentation/workloads-in-nvidia-run-ai/workload-types/supported-workload-types),
[Slurm 배포 예제](https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/tutorials/training-tutorials/deploying-slurm).
제품 문서 버전에 따라 기능 범위가 다를 수 있으며 비교 기준은 위 링크다.

**NAIS에 더 맞는가? [설계 해석]** 기존 기관별 Slurm/Kubernetes 정책을 유지하면서 공통 진입점을
만든다는 접근은 NAIS의 공동 활용과 기관 자율성에 연결해 설명할 수 있다. 그러나 이 실습이
Run:ai보다 기관 전체에 적합하다고 결론 내릴 근거는 없다. 실제 채택 판단에는 기관별 자원·사용자·
망·보안·정책 요구 및 운영비 비교가 필요하다. Run:ai를 일부 K8s 자원 계층에 사용하고 위에 공통
계층을 두는 조합도 검토 대상이다. **이 레포에는 Run:ai adapter와 직접 비교 실험이 없다.**
현재 제품 소개는 오픈소스를 안내하므로 상용·폐쇄형이라는 전제의 비교도 피한다.

---

## 8. 슬라이드 구성 제안 (선택)

1. 제목 — 분산된 연구자원을, 하나의 실행 흐름으로
2. NAIS가 하려는 것 [공식] — 1.1, 1.2
3. 목표 ↔ 해석 ↔ 구현 — 2장 표
4. 아키텍처 — 3.1·3.2 (기존 도구/구현 색 구분)
5. 실행 흐름과 상태기계 — 3.3
6. 프로젝트 구현 책임과 실제 본인 기여 — 4장·9장
7. 증거: Slurm 폐루프 — 5.1
8. 증거: K8s 5노드 — 5.2 (선택)
9. 범위와 한계 — 6장
10. 직무 연결 — 6.1
11. 부록 — 5.3 162작업, 7장 Run:ai

---

## 출처

### 공식 (2026-10-27 재확인)

- [NST 국가과학AI연구센터(NAIS) 소개][nst] — 비전, Hub/Spoke, 로드맵. `https://www.nst.re.kr/www/contents.do?key=358`
- [KTV “AI 과학기술 강국 도약”···국가 과학 프로젝트 ‘K-문샷’ (2026-05-06)][ktv] — `https://www.ktv.go.kr/content/view?content_id=753896`
- [NST NAIS 채용 직무기술서 ZIP][zip] — 연구기술직-3 플랫폼 PDF 포함. `https://www.nst.re.kr/www/downloadBbsFile.do?atchmnflNo=11753`
- [NST 보도자료 “출연연을 허브로 하는 AI 융합연구 본격 지원”][seed] — NAIS Seed형 지원. `https://www.nst.re.kr/www/selectBbsNttView.do?bbsNo=16&key=57&nttNo=52260`

### 로컬 구현·증거

링크는 이 파일 위치(`docs/nais-interview/`) 기준 상대경로. 핵심 목록은 [EVIDENCE-INDEX.md](EVIDENCE-INDEX.md), 공개 저장소 증거 파일 목록은 [DATA-FILES.csv](DATA-FILES.csv).

[nst]: https://www.nst.re.kr/www/contents.do?key=358
[ktv]: https://www.ktv.go.kr/content/view?content_id=753896
[zip]: https://www.nst.re.kr/www/downloadBbsFile.do?atchmnflNo=11753
[seed]: https://www.nst.re.kr/www/selectBbsNttView.do?bbsNo=16&key=57&nttNo=52260
[contracts]: ../../src/resource_advisor/contracts.py
[config]: ../../src/resource_advisor/configuration.py
[backends]: ../../src/resource_advisor/backends.py
[worker]: ../../src/resource_advisor/worker.py
[service]: ../../src/resource_advisor/service.py
[guard]: ../../src/resource_advisor/runtime_guard.py
[arch]: ../architecture.md
[gw]: ../reference/slurm-priority-gateway.md
[slurmrt]: ../reference/slurm-runtime.md
[slurmdoc]: ../reference/right-sizing-slurm-feedback.md
[slurmcsv]: ../evidence/right-sizing-slurm-attempts-v1.csv
[slurmaudit]: ../evidence/right-sizing-slurm-audit-v1.json
[resume]: ../evidence/pool-resume-readback-v1.json
[pool]: ../profile-pool-20261008-v2-results.md
[poolcsv]: ../evidence/profile-pool-20261008-v2-results/main.csv
[poolsum]: ../evidence/profile-pool-20261008-v2-results/summary.json
[jct]: ../jct-profile-queue-20261008-short-v1-recorded-results.md
[policy]: ../reference/policy-comparison-v2.md
[console]: ../reference/research-console.md

## 9. 본인 기여를 발표할 때 확인할 것

코드가 존재한다는 사실은 발표자가 그 코드를 혼자 작성하거나 모든 실험을 직접 운영했다는
증거가 아니다. 별도 로컬 실습 기록는 사용자 직접 실습과 도우미가 실행한 검증을
구분한다. AI/도구가 실행한 기록만으로 직접 운영 경력을 주장하지 않는다.

발표 준비 시 각 구현·실험 옆에 본인이 **요구사항 결정 / 설계 판단 / 구현 / 명령 실행 /
결과 해석 / 수정 검증** 중 실제 담당한 내용을 적는다. 이 문서는 그 기여 비율을 추정하지 않았다.
README의 “Slurm 없음” 등 9월 기록은 10월 Slurm 실험보다 앞선 상태이므로 최신 상태 설명에
그대로 사용하지 않는다. 반대로 과거 실험 성공을 현재 장비 가동 증거로 사용하지 않는다.

## 10. 자료 검증 기록

원본 코드·CSV·JSON·공식 문서와 대조해 작성했다. 새로운 클러스터 실험은 실행하지 않았다.
별도 Astra 검토 작업자는 시작 단계에서 `agent-hooks-review-prompt`로 차단되어 독립 검토를
완료하지 못했다. 아래 자료의 검증 상태는 작성·조정자의 원본 대조이며, 독립 검토 통과로 표시하지 않는다.
