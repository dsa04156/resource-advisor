# 발표용 이미지 자료

**최신 발표용 이미지:** [이미지 생성 도구로 만든 PNG 2종](imagegen/README.md). 아래 11종은 코드 기반 원본 도표다.

PNG는 3200×1800(16:9)이며 바로 PPT에 삽입할 수 있다. SVG는 벡터 도형·텍스트를 편집할 수 있다. SVG의 한글 글꼴은 Noto Sans CJK 계열이 필요하며 폰트가 없으면 PNG를 사용한다.

구조도는 코드 기반 재구성, 차트는 과거 기록의 시각화다. 실제 화면 캡처나 현재 가동 상태가 아니다. 중요한 표본·조건·한계는 그림 자체에도 표시했다.

10·11과 [오프라인 재생 데모](../offline-demo.html)는 비공개 실행 기록에서 허용 필드만 추출한 [정제 파생 데이터](../replay/observations.json)를 사용한다. 원본은 공개하지 않으며 해시·추출 범위는 [PROVENANCE.json](../replay/PROVENANCE.json)에 있다.

| 번호 | 내용 | PNG | SVG |
|---|---|---|---|
| 01 | 서로 다른 스케줄러를 공통 실행 계약으로 연결 | [PNG](01-architecture.png) | [SVG](01-architecture.svg) |
| 02 | 응답 유실을 “새 작업 제출”로 처리하지 않는다 | [PNG](02-submission-safety.png) | [SVG](02-submission-safety.svg) |
| 03 | NAIS의 목표 중, 공동 연산 운영 계층을 구현 | [PNG](03-nais-fit.png) | [SVG](03-nais-fit.svg) |
| 04 | Slurm 예약량과 연산 시간 | [PNG](04-slurm-loop.png) | [SVG](04-slurm-loop.svg) |
| 05 | K8s 성능 지표의 상충 | [PNG](05-k8s-tradeoff.png) | [SVG](05-k8s-tradeoff.svg) |
| 06 | 162작업 JCT 기록 | [PNG](06-jct-recorded.png) | [SVG](06-jct-recorded.svg) |
| 07 | 운영 역량은 정책 집행과 복구 기록으로 설명 | [PNG](07-operations.png) | [SVG](07-operations.svg) |
| 08 | NPU 품질 계약과 반복 실행 | [PNG](08-hailo.png) | [SVG](08-hailo.svg) |
| 09 | 비교의 중심은 “어느 운영 계층을 맡는가” | [PNG](09-runai-scope.png) | [SVG](09-runai-scope.svg) |
| 10 | 요청 10개를 K8s GPU·NPU와 별도 Slurm에 나누어 실행 | [PNG](10-fleet-integration.png) | [SVG](10-fleet-integration.svg) |
| 11 | 변동 도착·승격·재시도 관측 | [PNG](11-adaptive-timeline.png) | [SVG](11-adaptive-timeline.svg) |

## 그림별 설명

- **01-architecture**: 개념 구조도. backends.py, configuration.py, worker.py, service.py 근거. DB·S3·MLflow 전체가 단일 트랜잭션은 아니다.
- **02-submission-safety**: worker.py submit_one/reconcile 및 SubmissionUnknown 설계. 특정 실험의 실제 통신 추적 캡처가 아닌 코드 기반 설명도.
- **03-nais-fit**: NST NAIS 소개 https://www.nst.re.kr/www/contents.do?key=358 및 SOURCE-MATERIAL.md. NAIS 실제 배포 구조도가 아니다.
- **04-slurm-loop**: 원시 attempts CSV의 5개 행. compute는 측정 구간 합이며 개별 호출 지연이 아니다.
- **05-k8s-tradeoff**: summary.json 직접 사용. 준비·profiling 비용은 이 본 실행 차트에서 제외. 통계적 유의성 주장 없음.
- **06-jct-recorded**: recorded-results.json 값. 최종 감사 미완료를 그림 안에 명시. 서로 다른 부하를 합친 전체 요약.
- **07-operations**: slurm-policy.json, persistent-postgres.json, service-deployment.json. 사전 DB 행 수 655/52/201/8/52, 상세는 원자료.
- **08-hailo**: hailo-resnet50.md 표를 그대로 사용. 속도 개선·임의 NPU 호환성 주장 없음.
- **09-runai-scope**: Run:ai 공식 제품·scheduler·supported workload·Slurm tutorial 기준. 라이선스·비용·성능의 우열 비교 아님.
- **10-fleet-integration**: 정제 replay/observations.json의 fleet 마지막 프레임. 결과·MLflow 수신 10/10은 full-fleet-ten-request.json과 일치. 두 scheduler의 공동 GPU 소유가 아니다.
- **11-adaptive-timeline**: 정제 replay/observations.json의 adaptive 52개 프레임. 상태는 스냅샷 값 그대로이고 점 사이를 잇지 않는다. 시도 11·승격 6·worker 13은 adaptive-gpu-arrivals.json과 일치.

## 데이터와 출처

- [CHART-DATA.csv](CHART-DATA.csv): 그림에 표시한 주요 수치와 단위.
- [SOURCES.csv](SOURCES.csv): 데이터 원본 경로와 SHA-256. 경로는 레포 루트 기준.
- 10·11 정제 데이터 재생성(비공개 원본 필요): `rtk proxy python3 docs/nais-interview/replay/export_replay.py --fleet <RAW_FLEET_BATCH_JSON> --adaptive <RAW_ADAPTIVE_BATCH_JSON>`
- [전체 원자료](../SOURCE-MATERIAL.md), [증거 색인](../EVIDENCE-INDEX.md).
- 01·02 코드: src/resource_advisor/{configuration,backends,worker,service,contracts}.py
- 03 공식: https://www.nst.re.kr/www/contents.do?key=358
- 09 공식: https://www.nvidia.com/en-us/software/run-ai/
- 09 scheduler: https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/platform-management/runai-scheduler/scheduling/how-the-scheduler-works
- 09 NodeSet 범위: https://docs.nvidia.com/run-ai/self-hosted/documentation/workloads-in-nvidia-run-ai/workload-types/supported-workload-types
- 09 Slurm: https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/tutorials/training-tutorials/deploying-slurm

## 제작·재생성

build_visuals.py를 이용한 로컬 문서 산출 작업이며 시스템 구현을 변경하지 않는다. 그래프: Matplotlib, 도식: SVG/CairoSVG. 원본에서 수치 재독해, 표본·단위 확인, 이미지 육안 검수. 별도 실험 또는 독립 감사는 수행하지 않음.

한글 적용: Noto Sans CJK, 제목·본문 모두 한글 가독성 우선. paper #f5f5f5 / ink #2d3142 / accent #eb6c36 / muted #4f5d75. 데이터 축은 0에서 시작. 허용되지 않은 오차막대나 유의성 표시를 추가하지 않음.

시각화 절차 참고: Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026). Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents. https://doi.org/10.48550/arXiv.2609.00065 (확인된 최신 레코드 v2, 2026-09-02). 프로젝트의 실험 근거와는 별개인 제작 도구 참고문헌.
