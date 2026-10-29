# 발표 노트: Kubernetes와 가상 Slurm 위의 상위 control plane

- **용도:** NAIS 면접 데모 발표 (5–7분 본 발표 + 50–60초 압축 버전)
- **기준:** 2026-10-29. 근거와 상세는 [ARCHITECTURE-KO.md](ARCHITECTURE-KO.md), 출처는 [SOURCES.md](SOURCES.md)
- **그림:** `figures/01-overview.png`, `figures/02-kubernetes.png`, `figures/03-slurm.png` (16:9, root가 생성)

발표 원칙: 각 슬라이드에서 **[배포·관측] / [구현·미검증] / [오픈소스] / [계획]** 중 무엇인지 말로 밝힌다. 수치는 문서에 있는 것만 쓴다.

---

## 1. 5–7분 흐름

| # | 시간 | 화면 | 핵심 메시지 | 말할 근거 |
|---|---|---|---|---|
| 1 | 0:00–0:40 | 제목 + 한 문장 | "스케줄러를 새로 만든 게 아니라, 기존 스케줄러 위에서 요청·상태·사용량을 잃지 않게 하는 계층을 만들었습니다." | ARCHITECTURE 1절 |
| 2 | 0:40–1:50 | `01-overview.png` | 연구기관에는 K8s(컨테이너·서빙)와 Slurm(HPC 배치)이 공존합니다. RA는 공통 계약·route·PostgreSQL 장부·어댑터를 담당하고, 배치는 Kueue·kube-scheduler·slurmctld가 합니다. 08:09 UTC 기준 lab에는 API·worker·수집기 2개가 1/1로 떠 있습니다. | 3, 4절 |
| 3 | 1:50–3:00 | `02-kubernetes.png` | K8s는 get/list 권한으로 노드·Pod 요청량·Kueue를 30초마다 읽습니다. 제출은 LocalQueue 라벨을 단 Job이고, Kueue는 쿼터 안에서 들여보낼지만 정하고 노드는 고르지 않습니다. 노드는 kube-scheduler가 고릅니다. 쿼터 초과는 거절이 아니라 대기입니다. 미측정은 0이 아니라 unknown입니다. | 5절 |
| 4 | 3:00–4:20 | `03-slurm.png` | 가상 Slurm은 Docker 컨테이너 7개(컨트롤러+노드 6)입니다. RA는 SSH 22번 forced-command로만 들어가서 `docker exec`로 `scontrol`/`squeue` JSON 두 개만 읽습니다. REST·RPC 직접 연결은 없습니다. GPU 8개는 가상 슬롯입니다. SlurmDBD는 내부 네트워크에만 붙였습니다. | 6, 7, 13절 |
| 5 | 4:20–5:20 | 공통 Job 흐름 표 | 접수 → intent 커밋 → outbox lease → `SUBMITTING` 커밋 → native 호출 → reconcile → 종료+usage 한 트랜잭션. **가상 Slurm 제출은 일부러 막혀 있습니다**(worker 0 replica, runtime 바인딩 없음). | 9절 |
| 6 | 5:20–6:20 | 한계 매트릭스 | 실행 미검증, 새 native job 0, 미해결 불확실 제출 2건, 명령 경계일 뿐 root 격리 아님, HA 아님. | 12, 14절 |
| 7 | 6:20–7:00 | 다음 단계 | 전용 job UID → 적격 runtime → 2분 이하 smoke 1건 → 응답 손실 복구 시험. 그 전에는 관측 데모로 한정합니다. | 16절 Q12 |

### 슬라이드별 말하지 않을 것

- 2번: "GPU 풀을 통합했다" (아님 — 장부와 화면만 나란히)
- 3번: "Kueue가 노드를 골랐다", "이번에 GPU job을 돌렸다" (이번 업데이트 중 새 GPU job·재시작 복구 실험 없음)
- 4번: "A100 8장", "Slurm REST 연동", "root 소유 gateway"
- 5번: 가상 Slurm job이 실제로 흐르는 것처럼 화살표를 짚기
- 6번: "zero-loss 증명"

---

## 2. 50–60초 압축 스크립트

> Resource Advisor는 Kubernetes와 Slurm을 대체하지 않는 상위 control plane입니다. 쿠버네티스에서는 Kueue가 들여보낼지를, kube-scheduler가 노드를 정하고, Slurm은 slurmctld가 정합니다. 저희는 공통 Job 계약, 프로젝트별 route, PostgreSQL에 남기는 제출 intent와 사용량 장부, 그리고 응답이 사라졌을 때의 reconcile을 맡습니다.
>
> 지금 lab에는 API, worker, 쿠버네티스 수집기, 가상 Slurm 수집기가 떠 있습니다. 쿠버네티스는 읽기 권한으로 노드와 Pod 요청량, Kueue 큐를 30초마다 봅니다. 가상 Slurm은 Docker로 만든 6노드 클러스터인데, REST가 아니라 SSH forced-command로 들어가 허용된 JSON 조회 두 개만 실행합니다. GPU 8개는 가상 슬롯이라 성능 의미는 없습니다.
>
> 가상 Slurm 실행은 아직 열지 않았습니다. 전용 job 계정과 검증된 런타임이 없어서 worker를 0으로 두었고, 제출은 0건입니다. 다음 단계는 그 두 가지를 갖춘 뒤 짧은 smoke 한 건과 응답 손실 복구 시험입니다.

(약 55초, 분당 약 300자 기준)

---

## 3. 데모 순서 (화면이 있을 때)

1. Console 자원 화면: 클러스터 3개, `virtual-slurm-140` 타일 6개, "공유 슬롯" 표시. `lab-slurm`이 stale로 보이는 이유(2026-10-22 이후 수집 중단, 이력 보존)를 말한다.
2. API `GET /api/v1/compute/inventory/virtual-slurm-140` 응답에서 `sources`(`slurm_nodes: ok`, `slurm_queue: ok`, `prometheus: not_configured`)와 `status: partial`을 짚는다.
3. `deploy/slurm-execution-virtual-140/worker.yaml`의 `replicas: 0`과 `executor.json`의 `native_bindings: []`를 보여 주며 실행이 막힌 위치를 설명한다.
4. 화면에 토큰, 사설 IP, 호스트 키가 나오지 않게 한다. 터미널은 미리 자리표시자 버전으로 준비한다.

live 화면을 쓸 수 없으면 그림 3장과 [ARCHITECTURE-KO.md](ARCHITECTURE-KO.md)의 표로 대신한다. 이 경우 "현재 live 상태"라고 말하지 않고 "2026-10-29 기록"이라고 말한다.

---

## 4. 예상 질문 대응 요약

12개 질문과 답은 [ARCHITECTURE-KO.md 16절](ARCHITECTURE-KO.md#16-면접-예상-질문-12개)에 있다. 짧게 외울 핵심만 적는다.

| 질문 유형 | 한 줄 답 |
|---|---|
| 스케줄러냐 | 아니다. admission은 Kueue, 노드 배치는 kube-scheduler, Slurm은 slurmctld. RA는 장부·계약·reconcile |
| 실행됐냐 | 가상 Slurm은 아니다. 관측·accounting까지 |
| 왜 SSH냐 | slurmrestd 없음, forced-command로 명령을 좁힘 |
| GPU 8개 | 가상 슬롯, `/dev/null` 매핑 |
| 중복 제출 | intent 선커밋 + attempt 이름 reconcile, 재제출 안 함. 미해결 2건 있음 |
| 쿼터 | native 소유. Kueue는 대기, Slurm QOS는 거절 |
| 사용량 | 예약량이지 사용률 아님, unknown은 unknown |
| 보안 | 명령 경계. docker 그룹 계정이라 root 격리 아님 |
| partial | 메트릭 미연결, 장애 아님 |
| 사건 | ClusterID 불일치 ~90초, 롤백, 큐 비어 있었음 |
