# 이기종 자원 조합 검토 화면 (`#placement`)

상태: 소프트웨어 구현 + TEST DATA 격리 브라우저 검사. 실제 학습, native Job 제출, 클러스터 변경, 하드웨어 측정은 수행하지 않았다.

## 목적과 경계

연구자가 1–4개 단계에 **등록된 작업**을 고르고, 각 작업에 등록된 GPU·NPU 실행 후보(정확한 장치 모델·종류·백엔드·클러스터/노드·런타임·자원 키·등록 호환성)를 본 뒤, 단계 간 전송 요구를 입력해 `POST /api/v1/compute/placement-plans`로 자원 조합을 미리 검토한다.

- 미리보기 전용: Job 생성, 예약, 학습 실행 없음. 화면 하단 고정 문구 "배치 검토이며 작업을 실행하거나 자원을 예약하지 않습니다."
- 기존 단일 워크로드 제출(`#submit`)은 변경되지 않는다. 진입은 새 워크로드 화면 상단의 "이기종 자원 조합 검토" 링크와 작업 그룹 탭 "자원 조합 검토", 복귀는 "← 새 워크로드로 돌아가기".
- 임의 모델/이미지/명령 입력 없음. 후보는 서버 카탈로그(`submission_catalog`)와 서버 측 `compile_plan` 결과만 사용.

## 화면 구성

1. 스케줄링 정책(기존 프로필, 기본값은 단일 제출과 동일한 normal·Kubernetes 우선) + 프로젝트 토폴로지 스냅샷(`GET /topology-snapshots`, 측정·만료 시각, 출처, 측정 방법, 근거 ref, 엔드포인트/방향 링크 수).
2. 단계 카드(내부 role ID `stage-N`, 재사용하지 않음): 등록 작업 선택, 후보 표(체크 해제 시 `candidate_refs`로 제한), 스냅샷에는 있으나 이 작업에 실행 환경이 없는 장치는 "이 작업의 실행 환경 미등록". 학습 작업은 추론 검증으로 NPU 학습 가능을 추정하지 않는다는 안내.
3. 단계 사이 전송(생산자→소비자): 1회 전송량 MiB(요청에는 `round(MiB × 1,048,576)` bytes, 화면에 정확한 값 표시), 전송 횟수, soft/hard, 같은 노드, 선택적 최소 대역폭(Gbit/s ×125,000,000 또는 MiB/s ×1,048,576 B/s), 선택적 최대 요청 지연(ms). 대역폭 기본값을 가정하지 않는다. UI는 순차 edge만 만든다(API는 DAG 지원).
4. "배치 계획 확인" → 결과: 판정 헤더(평가 시각, 스냅샷 상태 유효/만료, 조합 평가/가능/제외 수, digest), 선택된 단계별 경로(장치 모델, 노드, 엔드포인트, 고정 장치 ref, 관측 여유; 링크의 측정 대역폭·요청 지연·측정 시각·근거와 별도 줄의 "예상 통신 … (추정)"), 단계별 후보와 판단(거절돼도 표시), 조합 순위(미측정/만료는 숫자 대신 "미측정 · 추정 불가"/"만료 · 추정 불가"), 제외 조합 이유 집계·상세, semantics, digest, "계획 JSON 내려받기"(서버 응답 원문).

## 상태 처리

- 입력·정책·스냅샷·단계·후보 변경 시 결과 즉시 무효화하고 이전 digest와 이유를 표시. 프로젝트 연결 해제 시 초안 전체 초기화.
- 요청마다 serial과 초안 key를 비교해 이전 초안의 늦은 응답은 버린다. 요청에는 `X-Request-ID`를 붙인다.
- 401/403/404/422/5xx는 구분된 한국어 메시지, 전송 실패는 초안 보존 + "다시 시도". 서버에 API가 없으면(404 `Not Found`) 사용 불가 설명과 기존 제출 링크만 표시. 스냅샷 0개는 명시적으로 안내하고 검토 버튼을 막는다.
- 사유 코드는 `역할 → 역할 · 설명 (부가값)`으로 번역하고 원본 코드는 `title`과 JSON에 남긴다.

## 검증 (TEST DATA)

- Fixture: `examples/placement_console_fixture.py` — `workload_console_fixture` 등록을 import 재사용, `TEST RTX 4090 (fixture)` 두 번째 GPU 노드, `fixture-gpu-head`, 학습 격리 바인딩이 없는 `fixture-cnn-training`, 유효/만료/다른 프로젝트 스냅샷. 링크 값(10 Gbit/s·2 ms, 1 Gbit/s·0.1 ms)은 순위 동작을 확인하기 위한 합성 입력이며 하드웨어 측정이 아니다. worker/native backend 없음, 임시 SQLite.
- Check: `rtk proxy uv run --with playwright python examples/placement_console_check.py output/playwright/placement-console` — 실제 FastAPI 앱에 대한 공개 흐름 57개 검사(Mobilint/GPU 실명, 미등록 장치, 정확한 bytes 변환, 작은 전송 다수→RTX 4090 / 큰 전송→RTX 5080 선택 변경, hard 최소 대역폭 제외, 만료 스냅샷 fail-closed, 학습 단계 거절과 후보 유지, 늦은 응답 무시, 전송 실패 초안 보존·재시도, 정책 변경 무효화, 4단계 상한, POST /jobs 0건, 390px·1366px overflow 없음, 빈 스냅샷, API 없는 서버, 기존 제출 정상). 라우트 가로채기는 실제 응답을 지연/차단만 하며 응답을 만들지 않는다.
- 미리보기: `rtk proxy uv run python examples/placement_console_fixture.py --port 18433` → `/console#placement`.

## 남은 일

- 실제 하드웨어 토폴로지 스냅샷·실제 학습 실행·캡처는 사용자 지시로 보류(미완료). fixture 수치는 발표 실적이 아니다.
- fixture에는 인벤토리 관측이 없어 모든 후보 여유가 "현재 여유 미확인"이며 `complete` 상태 경로는 브라우저에서 검증되지 않았다.
- 정적 자산 allowlist(`api.py`)는 백엔드 소유라 새 JS 파일 대신 `workspace.js`의 `createPlacementConsole` export로 구현했다.
- 401/403 분기는 코드상 처리만 있고 브라우저 검사로 재현하지 않았다.
