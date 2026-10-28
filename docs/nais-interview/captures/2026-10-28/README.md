# 면접 발표용 화면 캡처

2026-10-28 준비. PPT에 직접 넣는 PNG 원본과 발표 문장입니다. 완성된 슬라이드는 아닙니다.

## 먼저 사용할 이미지

| 발표 내용 | 추천 이미지 | 붙일 제목 / 설명 |
|---|---|---|
| 통합 작업 관리 | [06a-workload-table-focus.png](06a-workload-table-focus.png) | **이기종 작업의 공통 제출·조회 인터페이스** — CUDA·Hailo·Mobilint·Slurm 요청을 같은 목록에서 조회합니다. 화면의 4건은 격리된 테스트 API에 접수한 예시입니다. |
| 상위 계층의 판단 | [04-candidate-reasons-focus.png](04-candidate-reasons-focus.png) | **후보별 요구 자원과 선택 결과를 확인** — 호환 후보와 선택된 후보를 구분합니다. 이 테스트에는 여유 관측이 없으므로 이용률 기반 최적화의 증거로 쓰지 않습니다. |
| 장치와 실행 경로 | [03b-mobilint-review-focus.png](03b-mobilint-review-focus.png), [05a-slurm-review-focus.png](05a-slurm-review-focus.png) | **공통 요청을 백엔드별 실행 경로로 연결** — 왼쪽은 Mobilint의 NPU 요청·Kubernetes 큐, 오른쪽은 Slurm CPU 예시의 partition·QoS를 보여줍니다. |
| 실제 기록에 기반한 검증 | [08-recorded-execution-results.png](08-recorded-execution-results.png) | **Kubernetes·Slurm·NPU 실행 완료 기록** — 저장된 2026-10-07~08 실행 기록의 재생 화면입니다. 현재 live 상태가 아닙니다. |

## 발표 문장 예시

> 사용자는 공통 화면에서 작업과 정책을 선택합니다. 상위 계층은 등록된 실행 조건과 후보를 검토하고, 선택한 백엔드에 맞는 자원 요청과 큐 정보를 구성합니다. 실제 큐 처리와 노드 내 자원 배치는 Kubernetes/Kueue 또는 Slurm의 역할로 남겨 두었습니다.

> 새 제출 화면은 격리된 테스트 데이터로 제출·조회 흐름을 검증했습니다. 실제 장비 실행 근거는 별도로 보존한 10월 7~8일 기록을 제시합니다.

Run:ai 비교에서는 “공통 제출·검증 계층을 구현했다”는 범위를 설명합니다. 이 캡처만으로 Run:ai 대비 성능 우위, GPU 분할 공유, 공정 배분의 우수성을 주장하지 않습니다.

## 전체 파일

| 파일 | 보여주는 내용 |
|---|---|
| `01-workload-catalog.png` | 작업 유형과 등록된 CUDA·Hailo·Mobilint·Slurm 예시 |
| `02-auto-placement.png` | 자동 선택 정책과 제출 전 검토 전체 화면 |
| `03-mobilint-placement.png` | Mobilint 고정 선택 전체 화면 |
| `03a-device-selection-focus.png` | 자동 선택 / GPU / Hailo / Mobilint 선택 영역 확대 |
| `03b-mobilint-review-focus.png` | NPU·CPU·메모리·큐·시간 제한 검토 확대 |
| `04-candidate-reasons-focus.png` | 후보별 요구 자원, 관측 유무, 선택 결과 확대 |
| `05-slurm-placement.png` | Slurm CPU 예시 검토 전체 화면 |
| `05a-slurm-review-focus.png` | Slurm partition·QoS 및 자원 요청 확대 |
| `06-workload-submission-list.png` | 4개 테스트 요청을 접수한 목록 전체 화면 |
| `06a-workload-table-focus.png` | 장치 모델·실행 경로·요청 자원이 보이는 목록 확대 |
| `07-mobilint-submission-detail.png` | 접수한 Mobilint 테스트 요청의 상세 화면 |
| `07a-submission-progress-focus.png` | 요구사항·후보 검토·선택·큐·실행·결과 단계 표시 |
| `08-recorded-execution-results.png` | 기존에 저장한 실제 실행 기록의 재생 화면 원본 |

## 캡처의 근거와 한계

- `01`~`07a`: 실제 Console 코드와 public API를 사용한 **TEST DATA UI 검증**입니다. 캡처용 독립 SQLite와 fixture 등록만 사용했으며, 워커·네이티브 실행은 없습니다. 요청은 **접수됨 / 큐 제출 준비** 상태입니다. 실제 장비의 실행 완료나 성능 수치로 소개하지 않습니다.
- `08`: 기존 `../../visuals/mobilint-success-dashboard.png`를 수정 없이 복사했습니다. 2026-10-07 K8s 8건·Slurm 2건과 2026-10-08 Mobilint ARIES2 1건의 완료 기록 합본입니다. 11건을 동일 시각에 실행한 단일 실험으로 설명하지 않습니다. 근거는 [기존 자료 안내](https://github.com/dsa04156/resource-advisor/blob/6993e2d/docs/nais-interview/START-HERE.md#mobilint-포함-정상-실행-화면), [증거 목록](https://github.com/dsa04156/resource-advisor/blob/6993e2d/docs/nais-interview/EVIDENCE-INDEX.md), [정제 관측](https://github.com/dsa04156/resource-advisor/blob/6993e2d/docs/nais-interview/replay/observations.json)을 참조합니다.
- 테스트 화면의 `Mobilint ARIES`는 fixture 명칭입니다. 실제 기록의 장치는 `Mobilint ARIES2`입니다. Slurm UI 예시는 CPU 작업이며, 기존 실제 기록의 Slurm 작업은 Jetson Orin GPU 실행입니다.
- UI 캡처는 브라우저 조작과 기본 화면 캡처만 사용했습니다. DOM/CSS 수정, 상태 합성, 성공 결과 덧씌우기, 이미지 생성으로 화면을 만들지 않았습니다. `TEST DATA`와 관측 시점 표시를 유지하세요.
- 전체 화면은 기본 1920×1080 viewport, 2배 해상도(16:9 PNG는 3840×2160)입니다. 긴 화면은 세로 전체 캡처이고, `focus`는 브라우저 요소 캡처입니다. 후보 표 확대는 반응형 1100px viewport에서 촬영했습니다. 모든 파일을 16:9로 강제 잘라내지는 않았습니다.
- `capture-metadata.json`: 캡처 파일 해시, UI 소스 해시, 테스트 요청 접수 응답, 화면 오류 유무. `08`의 출처·해시는 별도 항목으로 기록합니다.
- 새 UI는 관련 Python 24개 및 기존 브라우저 52항목 통과 기록이 있습니다. 이번 캡처에서도 테스트 요청 4건 접수와 페이지 오류 0건을 확인했습니다. 필수 독립 리뷰는 Codex 시작 시 훅 신뢰 확인에 막혀 미완료이며, 리뷰 통과·운영 배포를 의미하지 않습니다.

이미지를 미리 보려면 [gallery.html](gallery.html)을 브라우저에서 엽니다. PPT에는 이미지 자체를 넣고, 표의 설명을 발표자 노트 또는 그림 설명으로 사용하세요.
