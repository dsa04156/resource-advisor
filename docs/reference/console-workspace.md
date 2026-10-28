# 작업 중심 Console

첫 사용자는 검증된 작업을 고르고 실행·결과로 이어집니다. 주 메뉴는 시작하기/작업/모델 서비스/자원/실험입니다. 기존 URL hash14개는 유지하며 관련 기능은 하위 탭과 운영·고급 도구로 연결합니다.

## 작업 흐름

홈은 첫 행동과 최근 작업을 보여줍니다. 새 작업은 서버의 curated catalog만 기본 카드에 표시하고, 정책/고정 자원/사용자 템플릿/원시 입력은 고급 영역으로 분리합니다. 제출은 기존 idempotency key와 서버 권한·큐 규칙을 그대로 사용합니다. 자동 선택은 호환성/관측된 예약 여유/정책을 따르며 실행시간 예측이나 예약 확정으로 표현하지 않습니다.

작업 목록은 표가 기본이며 검색·상태·백엔드 필터·페이지를 제공합니다. 상세는 요약/결과/실행 기록/자원·전체 기록으로 나뉩니다. 취소·재시도·MLflow 기록은 기존 API를 사용합니다. 실제 lifecycle_events를 보여주며 없는 단계 시각은 미확인으로 표시합니다.

## 이미지 체험

GET /api/v1/compute/inference-pools?page=0은 principal.project에 등록된 풀을12개씩 나눠 반환합니다. 내부 Service URL이나 자격증명은 반환하지 않습니다. 변경된 endpoint binding은 configured=false로 표시해 재등록이 필요한 풀의 실행을 막습니다. 목록은 readiness를 주장하지 않으며 선택한 풀의 health/identity를 기존 GET으로 별도 확인합니다.

현재 lab 풀은 ResNet50/CUDA ONNX/Hailo HEF의 검증된 입력/출력 계약을 따릅니다. 브라우저는 JPG/PNG/WebP의 최대512KiB 헤더에서 치수를 먼저 검사한 뒤 한 장≤8MiB·25MP을 RGB224×224로 stretch resize하며 투명 배경은 흰색입니다. WebP 애니메이션과 제한된 헤더에서 치수를 확인할 수 없는 파일은 거절합니다. 입력은 canvas pixels 0–255이고 서버가 네이티브 전처리를 적용합니다. 원본 이미지와 결과를 DB/스토리지에 저장하지 않습니다. 클라이언트에서 파일 선택과 결과를 유지하되 연결 해제/pagehide는 메모리·blob을 지우고 진행 요청을 abort합니다.

결과는 ImageNet 상위5개 분류/원시 모델 점수/실제 GPU 또는 Hailo 경로/라우터 구간 시간/fallback을 보여줍니다. 점수를 정확도나 정답 확률로 해석하지 않습니다. 분류명은 TensorFlow가 배포한 imagenet_class_index.json의 영어 라벨입니다. 선택한 이미지의 렌더링 예시는 새 품질 보장의 근거가 아닙니다.

풀 등록은 여전히 operator 전용이며 화면은 임의 URL/모델/이미지를 배포하지 않습니다. 기존 승인 후 모델 릴리스·staging·production 보호를 유지합니다. 새 이미지 체험은 그 릴리스와 별도 lab 기능입니다. 새 모델 lifecycle/릴리스 승인 편집 UI나 자동 재학습은 이번 범위에 포함하지 않습니다.

## 디자인 근거

- [SkyPilot 공개 작업 화면](https://github.com/skypilot-org/skypilot/tree/master/sky/dashboard): 검색/상태 필터/목록→상세
- [dstack 공개 프론트](https://github.com/dstackai/dstack/tree/master/frontend): 상세의 요약·지표·실행 기록 분리
- [ZenML 첫 안내 공식 소개](https://www.zenml.io/blog/oss-dashboard-release): 첫 행동을 단계로 안내 (2024 공식 예시)
- [GPUStack 공개 프론트](https://github.com/gpustack/gpustack-ui): 자원·모델서비스 영역만 참고

코드와 브랜드/서버는 우리 구현을 유지하며 참조 제품 코드를 이식하지 않았습니다. library icons는 lucide-static1.57.0에서 복사했고 static/icons/LICENSE에 MIT 고지를 포함합니다. 기본 스타일 토큰과 의도된 참조 차이는 .interface-design/system.md에 기록합니다.

기본 동작·프로젝트 경계·실제 GPU/Hailo 추론·배포 보존 확인은 [완료 근거](../evidence/console-workspace-20261014.json)에 기록했습니다. 전체 회귀·부하·장기·native 장애 주입은 사용자의 요청대로 후속입니다. 시각 비교와 의도된 변형은 [design-qa.md](../../design-qa.md)를 참고하세요.

## LAN 로딩 수정 (2026-10-14)

접속 서버는0.0.0.0:18420을 유지하고 API Service로 직접 연결합니다. 기존 CA와 인증서 identity를 검증하며 포트포워딩에 의존하지 않습니다. HTTP LAN 주소에서도 템플릿·파이프라인·재시도·시나리오·실험 버튼이128bit 요청 키를 생성할 수 있습니다. 기존 요청 키 재사용과 중복 제출 보호는 유지합니다.

최신 inventory 조회는 프로젝트·클러스터별 인덱스 탐색을 사용합니다. 과거 JSON 관측 전체를 읽거나 응답을 캐싱하지 않으며 기존 created_at/ref 최신순과 신선도 재평가를 유지합니다. 새 DB와 기존 DB의 init-db는 보조 index를 생성합니다. 큰 운영 DB에서 최초 적용은 다음 index를 별도 autocommit 연결로 생성한 뒤 코드를 배포합니다(현재 lab 적용 완료).

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ra_inventory_project_cluster_latest
ON ra_entities (project, (CAST(body->>'cluster_ref' AS VARCHAR)), created_at DESC, ref DESC)
WHERE kind = 'inventory';
```

초기 화면 데이터 조회는 기존6~13초에서 집중 확인 시0.5~0.8초로 줄었고, 동일 PostgreSQL snapshot에서 반환 장비 기록이 완전히 같은지 확인했습니다. [수정 및 배포 근거](../evidence/console-loading-fix-20261014.json)를 참고하세요.

## 워크로드 목록과 새 워크로드 흐름 (2026-10-28)

Run:ai v3.0.19 워크로드 관리자의 구조(목록 표·검색/필터·NEW WORKLOAD·맥락→설정→검토)를 참고해 `#jobs`와 `#submit`을 다시 구성했습니다. 참조 제품 코드·브랜드·인프라 정보는 사용하지 않았고, 기존 14개 hash route·인증·프로젝트 경계를 유지합니다.

- **워크로드(`#jobs`)**: 표(이름·유형·상태·장치/실행 위치·요청 자원·생성 시각), 상태·백엔드·장치(GPU/NPU/CPU 전용) 필터, 선택한 작업의 상세(요약/결과/실행 기록/자원·전체 기록, 취소·재시도·MLflow)를 1280px 이상에서는 오른쪽에, 그보다 좁으면 표 아래에 표시합니다. 선택과 상세 탭은 polling 후에도 유지되고, 목록 밖 작업은 `GET /jobs/{id}/view`로 다시 읽습니다.
- **새 워크로드 메뉴**: 서버 catalog에 등록된 작업 유형(추론·학습·성능 측정·전처리·저장한 템플릿)만 개수와 함께 보여줍니다. 임의 이미지·YAML 제출은 지원하지 않는다고 명시합니다.
- **새 워크로드(`#submit`)**: (1) 등록된 작업 카드, 각 카드에 실제 등록 후보 장치 모델·백엔드 표시 → (2) 실행 정책과 자동/고정 대상(호환되지 않는 후보는 이유와 함께 비활성) → (3) `POST /scheduling-plans` 검토: 프로젝트, 정책, 백엔드·큐, 예상 대상, 등록된 자원 요청(읽기 전용), 실행/대기 제한, 후보별 판단 이유 → (4) 기존 `POST /jobs`로 제출합니다. 검토가 수락되지 않으면 제출 버튼이 비활성이고 초안은 유지됩니다.
- **오류 처리**: 응답을 잃은 제출은 같은 Idempotency-Key로 "제출 확인 재시도"를 제공해 중복 접수되지 않습니다. overview API 연결이 끊기면 제출 UI 대신 연결 끊김 패널(보존된 초안 요약·다시 연결)을 표시하고 성공을 가장하지 않습니다. catalog가 비었거나 설정되지 않은 경우 그 사실을 문구로 설명합니다.

API 변경은 장치 필터에 필요한 최소 범위입니다: overview의 `jobs_device=all|gpu|npu|cpu`(기록된 capability `device_class` 기준, 페이지 전에 적용)와 job view의 읽기 전용 `device_class`·`accelerator_model` 필드입니다.

검증: `tests/test_console.py::test_job_device_filter_uses_recorded_capability`와 `examples/workload_console_check.py`입니다. 후자는 `examples/workload_console_fixture.py`의 **TEST DATA 등록**(CUDA/Hailo/Mobilint/Slurm 이름에 `TEST … (fixture)` 표기, worker 없음)으로 실제 FastAPI 앱을 띄우고 Chrome에서 PC 1366×768과 모바일 390×844를 확인합니다. 응답 유실·API 장애는 브라우저 안에서 응답을 끊는 방식으로만 재현합니다. fixture 결과는 실제 실행 근거가 아닙니다.

```sh
rtk proxy uv run --with playwright python examples/workload_console_check.py output/playwright/workload-console
```

화면만 살펴볼 때는 같은 TEST DATA로 격리 미리보기를 띄웁니다. 프로젝트 이름이 `TEST-DATA-fixture`로 표시되고 토큰 없이 그 프로젝트에만 연결됩니다. 임시 SQLite만 사용하며 worker·native backend·운영 API/DB에는 연결하지 않으므로 제출한 작업은 VALIDATED 상태에 머뭅니다. 토큰 로그인을 확인하려면 fixture 전용 값 `fixture-console-token`을 사용합니다(운영 자격 증명 아님).

```sh
rtk proxy uv run python examples/workload_console_fixture.py --host 0.0.0.0 --port 18432
```

데스크톱에서 검토 패널과 선택한 작업 상세는 92px 고정 상단바 아래(`top: 108px`)에 붙습니다. 이전에는 `top: 16px`이어서 스크롤하면 패널 상단이 상단바에 가려졌습니다. 1100px 이하에서는 검토 패널이 고정되지 않습니다.
