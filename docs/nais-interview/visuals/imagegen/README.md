# Imagegen presentation assets

내장 image_gen 도구로 생성한 PNG 2종. 기존 코드 기반 도표와 구분한 별도 버전이다.

- [통합 실행도](10-fleet-integration.png)
- [우선순위·복구 흐름도](11-recovery-flow.png)

두 번째 이미지는 관측 데이터에 기반한 설명용 흐름도이며, 원본 52시점 타임라인의 정밀 그래프가 아니다. 숫자는 원본 증거와 대조해 사용한다. source: docs/evidence/full-fleet-ten-request.json, docs/evidence/adaptive-gpu-arrivals.json. 노드·요청 대응의 상세 원본은 상위 폴더의 코드 기반 그림과 replay/observations.json을 참조한다.

## 생성 방식

Built-in image_gen, transparent_background=false, 기존 10/11 PNG를 factual reference로 첨부. CLI/API fallback 사용 안 함. 아래 최종 프롬프트를 사용했다.

## fleet

```text
Use case: infographic-diagram. Create a polished Korean technical interview presentation image, landscape 16:9, using the attached image as factual reference, redesigning its visual communication with the IMAGE GENERATION tool. This is for an NAIS research technical platform position interview. Make a beautiful, restrained editorial systems infographic: crisp Korean typography, generous white space, warm off-white background, deep navy text, slate blue execution arrows and a small orange emphasis. Flat clean technical illustration, restrained server/GPU pictograms; no 3D effects, no glow, no decoration, no branding logos. Make this visually designed rather than a spreadsheet screenshot. Exact Korean title: "하나의 요청 경로, 두 개의 실행 체계". Subtitle: "Kubernetes·Slurm·GPU·NPU 통합 실행 기록".
Main composition: common entry "공통 API · 작업 계약" with badge "요청 10개", branching clearly into TWO SEPARATE computing pools. Left larger pool heading "Kubernetes + Kueue", badges "요청 8개" and "GPU 6 · NPU 2". Its subtitle "K8s adapter → suspended Job → native admission". Show 5 GPU node pictograms plus 1 NPU node pictogram, label "누적 실행: GPU 노드 5개 + NPU 노드 1개". Right narrower pool heading "Slurm", badges "요청 2개" and "GPU 2". Subtitle "Slurm adapter → 제한 SSH → sbatch". Show 1 GPU node pictogram, label "누적 실행: GPU 노드 1개". Explicitly label dividing boundary "서로 다른 장비 풀".
Both flows rejoin in "결과 검증 · 실행 기록". Bottom evidence strip with FOUR carefully separated metrics: "성공 10건", "결과 10개", "MLflow 기록 10개", "사용량 기록 10개".
Bottom footnote exact text: "2026-10-07 기록 · 관측 스냅샷 24개 · 실행 노드 7개는 누적 관측이며 동시 할당 수가 아님"
Important invariants: 10 total requests, 8 Kubernetes and2 Slurm; 8 GPU requests and2 NPU requests total; 7 cumulative physical execution nodes =6 GPU and1 NPU. Do NOT imply simultaneous use, shared ownership of the same GPU by two schedulers, performance improvement, DDP, or production deployment. Do not reuse the reference's mistaken phrase 동시 제출. Do not invent extra numbers, latency, percentages, timestamps or charts. Readable minimal amount of text, all text must be correct.
```

## recovery

```text
Use case: infographic-diagram. Create a polished Korean 16:9 presentation infographic using the attached observed-timeline image as the factual reference. Redesign into a legible storytelling timeline explaining priority changes and bounded retry; this is an explanatory overview, NOT a reconstructed quantitative scatter plot. Keep the engineering evidence precise. Warm off-white background, deep navy Korean headings, slate blue execution elements, orange only for priority and injected failure; premium editorial technical illustration, high legibility, wide margins, clean lines, no 3D/glow, no fabricated graph points.
Exact title: "기다리는 작업을 조정하고, 실패한 시도는 남긴다"
Subtitle: "변동 도착 10건 · 우선순위 승격 · 다른 노드에서 1회 재시도"
Upper summary strip: "논리 요청 10개" / "실행 시도 11회" / "관측 시점 52개".
Main visual is a large left-to-right conceptual sequence of four stages:
1 "요청 도착" with "서로 다른 시점에 10건 접수".
2 "대기 · 우선순위 조정" with "20초 이상 대기한 pending 작업" and "6건: priority 10 → 100".
3 "기존 스케줄러 실행" with "Kueue admission" and "1 GPU 요청 7건 · 2 GPU 요청 3건".
4 "결과 확인" with "요청 10건 성공" and "CUDA worker 결과 13개".
Below, a distinct inset dedicated to logical request02, clearly linked to execution stage. Label "요청 02의 시도 이력". Show first attempt box "a1 · GPU-C" with orange failure icon "주입한 exit 42", then a retained evidence/document icon labelled "실패 기록 보존", then arrow labelled "기존 노드 제외 · 추가 시도 최대 1회", then second attempt box "a2 · GPU-A" with "성공". This inset must clearly keep both attempts within ONE logical request. Do not show exact failure/retry times because these are not provided here.
Bottom exact small-print boundaries: "2026-10-07 기록 · 설명용 흐름도: 단계 간 간격은 시간 비례가 아님" and "exit 42는 주입한 실패이며 하드웨어 장애가 아님 · 2 GPU는 독립 worker + 시작 barrier이며 DDP 학습이 아님".
Never invent success rates, latency improvements or event times. Do not redraw fake sampled dots or fill unobserved timeline gaps. Keep this a compelling, technically honest diagram derived from the supplied original evidence.
```
