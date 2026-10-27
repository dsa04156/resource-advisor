# Claude 전달용: NAIS 연구기술직-3 면접 원자료

## 사용자 목적

사용자는 NAIS 연구기술직-3(플랫폼·GPU/이기종 컴퓨팅 운영) 면접을 준비한다.
이 레포에서 Kubernetes와 Slurm을 어떻게 연결했는지, 설계 판단과 운영 증거를 설명하려 한다.
사용자가 PPT를 직접 구성할 수 있도록 설명 원자료·원본 데이터 링크·이미지를 제공한 상태다.
이 자료는 완성된 발표 덱이 아니다. 아래 자료를 먼저 읽고 사용자가 요청하는 후속 편집을 수행한다.

## 읽는 순서

1. [SOURCE-MATERIAL.md](SOURCE-MATERIAL.md): NAIS 목표/직무, 실제 구현, 상태 흐름, 측정값, Run:ai 비교와 한계.
2. [visuals/README.md](visuals/README.md): 9종 이미지의 용도와 PNG/SVG 링크.
3. [CONTACT-SHEET.png](visuals/CONTACT-SHEET.png): 이미지 전체 미리보기.
4. [EVIDENCE-INDEX.md](EVIDENCE-INDEX.md): 주장과 원본 증거 연결.
5. [DATA-FILES.csv](DATA-FILES.csv): 공개 저장소 증거 파일 목록.
6. [visuals/CHART-DATA.csv](visuals/CHART-DATA.csv), [visuals/SOURCES.csv](visuals/SOURCES.csv): 차트 수치·단위·원본 해시.

이미지를 한 번에 받으려면 [NAIS-visual-assets.zip](NAIS-visual-assets.zip)을 사용한다.
PNG 3200×1800, 벡터 SVG, 차트 데이터와 출처를 포함한다.
`gallery.html`은 내려받아 브라우저에서 열면 된다. GitHub에서는 HTML이 페이지로 렌더링되지 않을 수 있다.

## 먼저 이해할 핵심

- 공통 계약·API·project×cluster route → K8s/Kueue adapter 또는 별도 Slurm SSH adapter.
- 기존 scheduler가 배치를 결정한다. 같은 GPU를 두 scheduler가 함께 소유하는 구현은 아니다.
- 제출 intent → 불확실 상태 조정 → 결과 검증 → 상태·usage ledger 및 S3/MLflow 게시가 핵심이다.
- Slurm 고정 구성 5회, GPU 예약 421초는 폐루프 기능 증거다. 최적화 우월성을 뜻하지 않는다.
- K8s 10작업 실험은 연산 구간 감소와 평균 JCT 증가가 같이 관측됐다.
- 162작업 기록은 최종 독립 감사가 중단됐고 운영 정책에 반영하지 않았다.
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
