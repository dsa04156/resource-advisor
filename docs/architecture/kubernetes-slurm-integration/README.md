# Kubernetes × 가상 Slurm 통합 아키텍처 자료

Resource Advisor가 기존 Kubernetes와 가상 Docker Slurm 위에서 상위 control plane으로 동작하는 구조를 설명하는 NAIS 면접용 자료다. 기준 시각은 2026-10-29다.

| 파일 | 내용 | 작성 |
|---|---|---|
| [ARCHITECTURE-KO.md](ARCHITECTURE-KO.md) | 본 문서: 요약, 범례, 그림 3장, 배포 매핑, 프로토콜·인증 표, 관측 시퀀스, 공통 Job 흐름, 쿼터·사용량, 보안 경계, 인벤토리, 한계, 코드 맵, 예상 질문 12개 | 문서 작업자 |
| [PRESENTATION-NOTES-KO.md](PRESENTATION-NOTES-KO.md) | 5–7분 발표 흐름, 50–60초 스크립트, 데모 순서, 질문 대응 요약 | 문서 작업자 |
| [SOURCES.md](SOURCES.md) | 코드·배포·evidence 출처 색인과 증거 수준 | 문서 작업자 |
| [architecture.html](architecture.html) | 그림을 포함한 단일 파일 HTML | root가 생성 |
| [architecture.pdf](architecture.pdf) | 인쇄·첨부용 PDF | root가 생성 |
| [figures/01-overview.svg](figures/01-overview.svg) · [png](figures/01-overview.png) | 전체 구조 | root가 생성 |
| [figures/02-kubernetes.svg](figures/02-kubernetes.svg) · [png](figures/02-kubernetes.png) | Kubernetes 경로 | root가 생성 |
| [figures/03-slurm.svg](figures/03-slurm.svg) · [png](figures/03-slurm.png) | 가상 Slurm 경로 | root가 생성 |
| [build_assets.py](build_assets.py) | 그림·HTML·PDF 생성 스크립트 | root가 생성 |

HTML, PDF, 그림, `build_assets.py`는 root가 따로 생성·관리한다. Markdown과 그림의 문구가 다르면 [SOURCES.md](SOURCES.md)의 원본 evidence를 기준으로 확인한다.

## 읽는 순서

1. [ARCHITECTURE-KO.md 1–2절](ARCHITECTURE-KO.md#1-한-페이지-요약): 한 페이지 요약과 표기
2. 그림 3장과 같은 문서 3–6절
3. [ARCHITECTURE-KO.md 14절](ARCHITECTURE-KO.md#14-한계-매트릭스): 말해도 되는 것과 안 되는 것
4. [PRESENTATION-NOTES-KO.md](PRESENTATION-NOTES-KO.md)

## 핵심 경계

- RA는 스케줄러가 아니고 GPU를 하나의 물리 풀로 합치지 않는다.
- 가상 Slurm은 **관측과 accounting만 배포**됐다. 실행 route는 예시 설정이고 worker는 0 replica이며 제출은 0건이다.
- 가상 GPU 8개는 `/dev/null` 매핑 가상 슬롯이다.
- 공개 문서에는 실제 IP·토큰·비밀번호·SSH fingerprint를 쓰지 않는다(`<K8S_API>`, `<SLURM_HOST>`, `<ADVISOR_EGRESS_IP>`, `<RA_API>`).
