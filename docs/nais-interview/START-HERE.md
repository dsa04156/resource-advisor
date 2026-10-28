# Claude 전달용: NAIS 연구기술직-3 면접 원자료

## 사용자 목적

사용자는 NAIS 연구기술직-3(플랫폼·GPU/이기종 컴퓨팅 운영) 면접을 준비한다.
이 레포에서 Kubernetes와 Slurm을 어떻게 연결했는지, 설계 판단과 운영 증거를 설명하려 한다.
사용자가 PPT를 직접 구성할 수 있도록 설명 원자료·원본 데이터 링크·이미지를 제공한 상태다.
이 자료는 완성된 발표 덱이 아니다. 아래 자료를 먼저 읽고 사용자가 요청하는 후속 편집을 수행한다.

## 실증 기록 기반 관리 화면 — 발표 우선 자료

- [관리 화면](management/index.html), [캡처 갤러리](management/captures/gallery.html), [PNG 8장 ZIP](NAIS-management-captures.zip).
- [관리 화면 설명·발표 문장·원본 출처](management/README.md)를 먼저 읽는다.
- Run:ai처럼 **개요 → 자원 → 워크로드 → 대기열·실행 → 결과**를 이어서 설명한다. 모빌인트를 포함한 장치 모델, Kubernetes·Slurm 경로, 요청별 상태, Slurm 대기 사유, 결과·MLflow 수신을 볼 수 있다.
- 2026-10-07~08 실제 하드웨어 기록을 새 관리 화면으로 렌더링했다. 완료 요청 11건(K8s 9·Slurm 2), 누적 실행 노드 8개(GPU 6·NPU 2)이며, 현재 가동 상태나 과거 운영 UI 자체의 캡처는 아니다.
- 관리 흐름을 설명하는 발표에는 이 자료를 우선한다. 아래 작업 제출 화면은 화면 기능 설명에 사용한다.

## 작업 제출 화면 캡처 (2026-10-28)

- [캡처 ZIP](NAIS-interview-captures-20261028.zip): 고해상도 PNG 13장, 발표 문장, 출처·해시.
- [캡처별 용도와 발표 문장](captures/2026-10-28/README.md), [이미지 미리보기](captures/2026-10-28/gallery.html).
- 새 Console의 작업 선택·자동 배치 검토·Mobilint 선택·Slurm 검토·통합 접수 목록을 촬영했다. `01`~`07a`는 격리된 **TEST DATA UI 검증**이며 실제 장비 실행은 없다.
- `08`은 기존 2026-10-07~08 실제 실행 기록 재생 캡처다. 새 UI의 접수 예시와 실제 실행 기록을 구분해 설명한다.

## 아키텍처 이미지: 이미지 생성 도구로 만든 버전

사용자는 이미지 생성 기능으로 만든 발표용 그림을 요청했다. **아래 두 PNG를 발표 편집의 우선 시각 자료로 사용한다.**

- [통합 실행도 PNG](visuals/imagegen/10-fleet-integration.png): 공통 요청에서 Kubernetes·Slurm 실행과 결과 기록까지.
- [우선순위·복구 흐름도 PNG](visuals/imagegen/11-recovery-flow.png): 요청 하나 안에 실패와 다른 노드 재시도 이력을 보존하는 구조.
- [이미지 생성 버전 ZIP](NAIS-imagegen-assets.zip): PNG 2종과 최종 생성 프롬프트.
- [생성 방식·프롬프트](visuals/imagegen/README.md): 내장 image_gen 사용. 두 번째 그림은 설명용 흐름도이며 정밀 시간축 그래프가 아니다.

기존 `visuals/01~11`은 코드 기반 원본 도표로, 수치·관측 시점 확인에 사용한다.
이미지 생성 버전과 기존 도표의 숫자·문구가 다르면 원시 증거와 계약 정의를 기준으로 확인한다.

## 읽는 순서

1. [SOURCE-MATERIAL.md](SOURCE-MATERIAL.md): NAIS 목표/직무, 실제 구현, 상태 흐름, 측정값, Run:ai 비교와 한계.
2. [visuals/README.md](visuals/README.md): 11종 이미지의 용도와 PNG/SVG 링크.
3. [CONTACT-SHEET.png](visuals/CONTACT-SHEET.png): 이미지 전체 미리보기.
4. [offline-demo.html](offline-demo.html): 저장된 관측을 재생하는 오프라인 데모(네트워크 불필요, 파일을 더블클릭해 연다). live 상태가 아니다.
5. [EVIDENCE-INDEX.md](EVIDENCE-INDEX.md): 주장과 원본 증거 연결.
6. [DATA-FILES.csv](DATA-FILES.csv): 공개 저장소 증거 파일 목록.
7. [visuals/CHART-DATA.csv](visuals/CHART-DATA.csv), [visuals/SOURCES.csv](visuals/SOURCES.csv): 차트 수치·단위·원본 해시.

이미지를 한 번에 받으려면 [NAIS-visual-assets.zip](NAIS-visual-assets.zip)을 사용한다.
PNG 3200×1800, 벡터 SVG, 차트 데이터와 출처를 포함한다.
`gallery.html`은 내려받아 브라우저에서 열면 된다. GitHub에서는 HTML이 페이지로 렌더링되지 않을 수 있다.

## 상위 스케줄링 설명 위치

[SCHEDULER-SLIDE-NOTES.md](SCHEDULER-SLIDE-NOTES.md)는 **Kubernetes·Slurm 연결과 하위 스케줄러의 작업 처리**를 쉽게 설명하는 5장 분량 원고다. 슬라이드별 본문·그림 배치·발표 멘트·코드 근거가 있다. Kubeflow는 요청 진입점으로 짧게 다루고, 공통 계층 → 어댑터 → Kueue/kube-scheduler 또는 Slurm → 상태·결과 회수를 중심으로 구성한다.

[SOURCE-MATERIAL.md 3.4절](SOURCE-MATERIAL.md#34-상위-스케줄링-계층-무엇을-보고-어디까지-결정하는가)을 먼저 읽는다. 후보 필터 → 관측 여유 → backend 선호 → 진행 중 접수 수 → 최근 5분 접수 수 → candidate ref 순의 선택 규칙, project별 동시 접수 lock, 계획 검증, adapter와 native scheduler의 책임 경계를 정리했다. 완료 화면은 실행 증거이며 선택 이유의 증거는 저장된 scheduling plan이다.

## 먼저 이해할 핵심

- 공통 계약·API·project×cluster route → K8s/Kueue adapter 또는 별도 Slurm SSH adapter.
- 기존 scheduler가 배치를 결정한다. 같은 GPU를 두 scheduler가 함께 소유하는 구현은 아니다.
- 제출 intent → 불확실 상태 조정 → 결과 검증 → 상태·usage ledger 및 S3/MLflow 게시가 핵심이다.
- Slurm 고정 구성 5회, GPU 예약 421초는 폐루프 기능 증거다. 최적화 우월성을 뜻하지 않는다.
- K8s 10작업 실험은 연산 구간 감소와 평균 JCT 증가가 같이 관측됐다.
- 162작업 기록은 최종 독립 감사가 중단됐고 운영 정책에 반영하지 않았다.
- 10개 요청 통합 실행(K8s GPU·NPU + Slurm)과 우선순위 승격·주입 실패 재시도는 2026-10-07 lab 기록이다. 실행 노드 7개는 누적이며, exit 42는 주입 실패다.
- 그림 10·11과 데모는 비공개 원본에서 허용 필드만 추출한 [정제 데이터](replay/observations.json)를 쓴다. 원본은 공개하지 않는다.
- Run:ai보다 낫다고 결론 내리지 않는다. 공통 연결·검증 계층과 자원 배분 계층의 역할을 비교한다.

## 자료를 재사용할 때

공식 목표 / 설계 해석 / 구현 / 검증됨 / 미검증을 구분한다.
예약초·슬롯초·연산시간·JCT·이용률을 바꾸어 쓰지 않는다.
코드 존재나 도우미 실행 기록을 사용자의 단독 구현·직접 운영 경험으로 바꾸지 않는다.
그림은 코드·기록 기반 재구성이다. 실제 서비스 캡처나 현재 live 상태가 아니다.
독립 Astra 검토 작업자는 시작 단계에서 차단되어 독립 검토 통과 상태가 아니다.
새 숫자·실험·배포 완료 사실을 만들어 넣지 말고, 근거가 부족하면 필요한 증거를 구체적으로 적는다.

## 코드에서 직접 확인할 위치

- [configuration.py](../../src/resource_advisor/configuration.py): project×cluster route
- [backends.py](../../src/resource_advisor/backends.py): KubernetesBackend / SlurmBackend
- [worker.py](../../src/resource_advisor/worker.py): 제출 intent, UNKNOWN, reconcile, 결과 수집
- [service.py](../../src/resource_advisor/service.py): 요청/결과 계약 검증
- [contracts.py](../../src/resource_advisor/contracts.py): 불변 계약·상태·결과 형식
- [architecture.md](../architecture.md): 경계와 운영 한계

이 디렉터리는 발표 자료만 추가한다. 다른 미커밋 개발 변경과 로컬 비공개 실습 폴더는 포함하지 않는다.

## 후속 Claude 작업 안내

먼저 원자료와 이미지·데모를 읽고, NAIS 목표 → 설계 선택 → 실제 동작 → 운영 증거 → 적용 한계 순으로 설명을 정리한다.
사용자가 PPT를 직접 구성한다는 기존 의도를 유지하되, 이후 사용자가 슬라이드 제작을 요청하면 그 요청을 따른다.
현재 요청은 후속 자료 전달이며 live 실험·배포·클러스터 변경을 포함하지 않는다.

오프라인 데모는 데이터 검사 14개 및 브라우저 검사 46개를 통과한 기록이 있다.
독립 Astra 검토는 Orca 시작 단계에서 차단됐다. 이번 Git 게시가 독립 검토 완료를 의미하지 않는다.

## Mobilint 포함 정상 실행 화면

[정상 실행 결과 보기](offline-demo.html#success)는 K8s 8건·Slurm 2건과 Mobilint ARIES2 성공 기록을 한 표의 11행으로 표시한다. Mobilint는 2026-10-08 Candy 실행 기록이며, 각 행의 실행일로 2026-10-07 통합 실행 10건과 구분하며, 합본 표의 요약은 두 날짜의 11건을 집계한다. 원본은 하단의 Mobilint 실행 근거 링크에서 고정 커밋으로 확인할 수 있다. 기존 재시도 기록은 원본 그대로 유지한다.

[정상 실행 화면 캡처](visuals/mobilint-success-dashboard.png)를 PPT에 사용할 수 있다.

Mobilint 행과 NPU-B 노드는 fleet의 처음 관측부터 끝까지 계속 표시한다. 10월 8일 완료 기록으로 고정하며, 10월 7일 재생 시간축에 맞춘 대기·실행 상태는 생성하지 않는다.
