# 실증 기록 관리 화면 (2026-10-07~08)

이 폴더는 **저장된 실제 하드웨어 실행 기록을 2026-10-28에 새로 렌더링한 읽기 전용 관리 화면**입니다.
과거 운영 UI의 스크린샷도, 현재 live 상태도 아닙니다. 서비스·API·DB·worker를 다시 띄우거나 작업을 실행하지 않았습니다.
화면 정보 구조(개요 → 자원 → 워크로드 → 대기열·실행 → 결과)는 Run:ai 관리 콘솔을 참고했고, 디자인 토큰은 HAIRP Console(`.interface-design/system.md`)을 따릅니다. NVIDIA 브랜딩은 쓰지 않았습니다.

- 보기: 기존 정적 서버 `http://127.0.0.1:18421/management/` (서버 root = `docs/nais-interview`)
- 경로: `#/overview`, `#/resources`, `#/workloads/R10?backend=slurm&device=gpu`, `#/queue/12`, `#/results`
- 캡처 갤러리: [captures/gallery.html](captures/gallery.html), ZIP: [../NAIS-management-captures.zip](../NAIS-management-captures.zip)

## 발표용 캡처

PNG는 1920×1080 화면을 2배(3840×2160)로 촬영했습니다. `03a`·`03b`·`04a`는 같은 화면의 요소 crop입니다.

| 파일 | 슬라이드 제목 / 한 줄 설명 |
|---|---|
| `01-overview.png` | **이기종 실행 완료 기록 개요** — 2026-10-07~08 완료 기록 11건(백엔드별 완료 요청: K8s 9건 · Slurm 2건), 실행 노드 8개(누적, GPU 6 · NPU 2), 결과·MLflow 수신 11/11. |
| `02-resources.png` | **실행된 자원과 할당 단위 경계** — RTX 5060 Ti·RTX 5080·GB10·Jetson·Hailo-8·Mobilint ARIES2·Slurm Orin. 전용 GPU·공유 슬롯·NPU·GRES를 합산하지 않습니다. |
| `03-workloads.png` | **요청 단위의 공통 작업 관리** — 11건을 백엔드·장치로 필터하고 행별 기록을 엽니다. |
| `03a-workload-detail-R10.png` | **Slurm 대기 사유와 단계 기록** — R10은 `AssocGrpCpuLimit`으로 대기한 뒤 실행·결과 수집을 거쳐 완료되었습니다. |
| `03b-workload-detail-R11.png` | **Mobilint NPU 실행 기록** — 2026-10-08 Kubernetes 실행, NPU 1·CPU 1·1024 MiB 요청, MLflow 수신. |
| `04-queue-snapshot-12.png` | **한 시점의 대기·실행 상태** — 2026-10-07 04:29:54.303 UTC 저장 스냅샷: 완료 4 · 검증 2 · 실행 2 · 대기 2. Kueue 네이티브 카운터(admitted 0 · pending 0)는 별도 계층의 값입니다. |
| `04a-queue-board-12.png` | 같은 스냅샷의 상태 보드 crop. |
| `05-results.png` | **결과 수신과 하드웨어 측정 경계** — Mobilint 1건의 측정값과 11건의 결과 수신. 장치 간 속도 비교가 아닙니다. |

## 읽을 때 지켜야 할 경계

화면에는 "실증 기록 · 2026-10-07~08"·"관측 기록 기준" 배지와 짧은 캡션만 두었습니다. 아래 한계는 화면에서 반복하지 않고 여기에만 적습니다.

- **live 아님 · 읽기 전용**: 저장된 기록을 다시 그린 화면입니다. 현재 상태가 아니며 제출·취소 기능이 없습니다.
- **기록 범위**: 왼쪽 아래 "이기종 실증 기록"은 fleet_batch(2026-10-07)와 Mobilint(2026-10-08) 두 수집 기록을 뜻합니다. 두 기록이 같은 project에서 실행됐다는 근거는 확인하지 않았으므로 project 이름을 표시하지 않습니다.
- **실행 경로 표시**: R01–R08과 해당 장치는 `K8s · Kueue`로 표시합니다. 2026-10-07 fleet_batch 실행에는 Kueue `admitted/pending` 관측이 스냅샷 12–23에 남아 있습니다(실행 단위 관측이며, 요청별 admission 기록은 아님). Mobilint `jobs[0]`에는 `backend: kubernetes`와 컨테이너 요청 기반 할당만 기록되어 있고 Kueue 관련 필드가 없습니다. 그래서 R11과 NPU-B는 모든 화면에서 `Kubernetes`로만 표시합니다. `build_data.py`가 두 조건을 assert합니다.

- **누적 vs 동시**: 11건은 2026-10-07 fleet_batch 10건과 2026-10-08 Mobilint 1건을 합친 것입니다. 한 번에 동시에 실행된 배치가 아닙니다. 실행 노드 8개도 두 날짜의 누적이며 온라인 용량·동시 사용률이 아닙니다.
- **자원 화면**은 기록에서 실제로 실행된 장치의 이력입니다. CPU·메모리 총량, GPU 사용률, 쿼터, fair share, 정상/online 상태는 이 기록에 없어서 표시하지 않습니다.
- **대기열 화면**은 저장된 24개 스냅샷 중 선택한 것만 보여 주며 스냅샷 사이를 보간하지 않습니다. 사유 코드는 해당 스냅샷 기록에 있을 때만 표시합니다. 공통 요청 상태 `QUEUED`와 Kueue `admitted/pending`은 서로 다른 계층의 값입니다. 흐름도는 구조 설명이며 시각 기록이 아닙니다.
- **결과 화면**: `반복 출력 원소 일치 1.0`(`finite_output_repeat_element_agreement`)은 고정 입력을 반복 실행했을 때 출력이 같은지 보는 지표이며 추론 정확도가 아닙니다. 처리량 단위는 work units/s(10 work units ÷ 측정 연산 구간)입니다. 가속기 사용률·전력·온도는 기록되지 않았습니다. R01–R10의 결과 품질값은 워크로드별 의미가 달라 화면에 표시하지 않았습니다.
- 성공한 결과만으로 선택 정책의 효과를 주장하지 않습니다. 선택 이유는 저장된 scheduling plan에서 따로 확인해야 합니다([SOURCE-MATERIAL 3.4](../SOURCE-MATERIAL.md#34-상위-스케줄링-계층-무엇을-보고-어디까지-결정하는가)).

## 출처와 재생성

| 역할 | 파일 | SHA-256 |
|---|---|---|
| 2026-10-07 fleet_batch 스냅샷 24개 · R01–R10 | [../replay/observations.json](../replay/observations.json) (manifest [PROVENANCE.json](../replay/PROVENANCE.json)) | `eb0b39d3494146521fc74ae621ead4265d3e75fb02643b0a2f53631c995cf218` |
| 2026-10-08 Mobilint ARIES2 `jobs[0]`, `qualifications[0].report` | [all-accelerators-20261008-v1.json @ a5d1dde](https://github.com/dsa04156/resource-advisor/blob/a5d1dde87b56ba2794dc6a93559342eb85f1940f/docs/evidence/all-accelerators-20261008-v1.json) | `64ebe1c9d4e585f59e0e7d7b55665d3758d031a0924c2426fb4de6c6ff44bd09` |
| 장치 모델 매핑 | [../replay/DEVICE-MODELS.md](../replay/DEVICE-MODELS.md) | — |

- `NPU-B`는 Mobilint 기록의 `node_alias npu-1`에 붙인 공개용 별칭이고, `R11`은 합본 표의 표시 번호입니다(10월 7일 원본의 요청 번호가 아님).
- Mobilint 기록에서는 허용 필드만 추출했습니다. job/attempt/native/MLflow 식별자, digest, 서비스 주소, 자격 증명은 `data.json`에 없습니다. Mobilint 기록에는 Kubernetes 리소스 이름이 없어서 화면에 "리소스 이름 미기록"으로 표시합니다.
- 재생성 (저장소 root에서):
  - `rtk proxy python3 -I docs/nais-interview/management/build_data.py` — `data.json`을 만들고, 표시되는 모든 집계를 원본과 대조해 assert합니다.
  - `rtk proxy uv run --isolated --with playwright python docs/nais-interview/management/capture.py docs/nais-interview/management/captures` — 경로 탐색, 필터·상세 동작, 24개 스냅샷 대조, 1920/390 가로 넘침, 오류 검사를 하고 캡처를 찍습니다. 결과는 [captures/capture-metadata.json](captures/capture-metadata.json)에 있습니다(96/96 통과, page/console/http 오류 0).
- 캡처는 서버가 제공한 페이지를 그대로 촬영했습니다. 촬영용으로 DOM이나 CSS를 고치지 않았습니다.

## 검토 상태

독립 Codex 리뷰는 hook trust 단계(agent_readiness)에서 막혀 실행되지 않았습니다. **독립 리뷰는 아직 대기 중**이며, 이 화면은 독립 리뷰를 통과한 상태가 아닙니다.
