# 출처 색인

[ARCHITECTURE-KO.md](ARCHITECTURE-KO.md)와 [PRESENTATION-NOTES-KO.md](PRESENTATION-NOTES-KO.md)의 주장이 기대는 파일 목록이다. 증거 수준은 다음과 같다.

- **live**: lab에 배포하고 readback·API·명령 결과로 확인한 고정 기록
- **code/test**: 코드와 관련 자동 테스트. 실제 클러스터 실행 아님
- **config**: 배포 manifest·설정 파일. 존재가 곧 배포를 뜻하지 않음
- **history**: 이번 가상 Slurm 데모 이전의 실장비 기록

## 고정 evidence (2026-10-29)

| 파일 | 수준 | 쓰인 주장 |
|---|---|---|
| [docs/evidence/virtual-slurm-140-inventory-20261029.md](../../evidence/virtual-slurm-140-inventory-20261029.md) | live | 6노드·8슬롯(4/2/1/1), 07:06:50Z 스냅샷 `partial`, 클러스터 3개(12/2 stale/6), observer 키 허용·거부 목록, `/dev/null` 가상 장치 |
| [docs/evidence/slurm-execution-140-20261029.md](../../evidence/slurm-execution-140-20261029.md) | live + code/test | SlurmDBD 24.11.5·MariaDB 11.8.9, association `sim/ra-lab/slurm`, ClusterID 623→927 사건, executor/results 키 읽기 전용 확인, 새 job 0, RA 행 수 878/3918/876, 실행 NOT_VALIDATED |
| [docs/experiments/integrated-operations/evidence/deployment-20261029.json](../../experiments/integrated-operations/evidence/deployment-20261029.json) | live | `ra-api` revision 96, 이미지 소스(커밋 `53cd73b` + 보존 모듈 5개), 이력 해시 불변, `SUBMISSION_UNKNOWN` 2건, smoke `NOT_RUN`, 삭제 사건 |

배포 상태 07:58 UTC(API·worker·수집기 2개 모두 1/1)는 coordinator가 전달한 readback이며, 이 브랜치에는 그 시각의 별도 evidence 파일이 없다. 07:43Z 기록의 같은 Deployment 1/1 값은 위 slurm-execution evidence에 있다.

## 설계·결과 문서

| 파일 | 수준 | 쓰인 주장 |
|---|---|---|
| [docs/experiments/integrated-operations/ARCHITECTURE.md](../../experiments/integrated-operations/ARCHITECTURE.md) | code/test | lifecycle·accounting·policy 계약, 대기 사유 분류, route당 worker 1개 |
| [docs/experiments/integrated-operations/RESULTS.md](../../experiments/integrated-operations/RESULTS.md) | code/test + live | 테스트 목록, UID precondition 취소, live 배포 결과, 삭제 사건 |
| [docs/experiments/integrated-operations/CURRENT-STATE-AUDIT.md](../../experiments/integrated-operations/CURRENT-STATE-AUDIT.md) | code/test | 기존 기능 목록, 갭 G1–G8, KAI·SlurmREST 범위 외 |
| [docs/reference/operations-ko.md](../../reference/operations-ko.md) | 설명 | 운영 화면 사용 순서, 예약량과 사용률 구분 |
| [docs/reference/inventory.md](../../reference/inventory.md), [docs/evidence/inventory-live.json](../../evidence/inventory-live.json) | history | Prometheus로 DCGM·Jetson·Mobilint 관측 |
| [docs/reference/common-gpu-pool.md](../../reference/common-gpu-pool.md) | config | 공유 슬롯은 물리 장치 수가 아님 |
| [docs/reference/kueue-policy.md](../../reference/kueue-policy.md) | history | Kueue 쿼터 초과 = admission 대기 |
| [docs/reference/slurm-jetson-runtime-results.md](../../reference/slurm-jetson-runtime-results.md), [docs/reference/slurm-vm.md](../../reference/slurm-vm.md) | history | 기존 Slurm backend의 과거 실행. 가상 140과 별개 |

## 배포 설정

| 경로 | 수준 | 내용 |
|---|---|---|
| [deploy/services/services.yaml](../../../deploy/services/services.yaml) | config (배포됨) | `ra-api` 8443, `ra-inventory` 30 s, Service |
| [deploy/inventory-rbac.yaml](../../../deploy/inventory-rbac.yaml) | config (배포됨) | 수집기 get/list 권한 |
| [deploy/slurm-inventory-virtual-140/](../../../deploy/slurm-inventory-virtual-140/README.md) | config (배포됨) | 가상 Slurm 수집기, observer scope, Secret 이름, 롤백 |
| [deploy/slurm-accounting-virtual-140/](../../../deploy/slurm-accounting-virtual-140/README.md) | config (배포됨) | slurmdbd·MariaDB, `slurm.conf` 4줄, ClusterID 처리, 롤백 |
| [deploy/slurm-execution-virtual-140/](../../../deploy/slurm-execution-virtual-140/README.md) | config (**미배포**, replicas 0) | executor/results scope, 예시 worker route, 실행 불가 이유 |

## 코드

| 파일 | 쓰인 부분 |
|---|---|
| [src/resource_advisor/api.py](../../../src/resource_advisor/api.py) | `PREFIX = "/api/v1/compute"`, Bearer 인증, `/jobs`, `/inventory/{cluster_ref}`, `/overview`, `/usage/report`, `/jobs/{id}/lifecycle`, `/jobs/{id}/submission-resolution`, `/healthz` |
| [src/resource_advisor/console.py](../../../src/resource_advisor/console.py) | `overview`의 인벤토리 읽기 모델 |
| [src/resource_advisor/service.py](../../../src/resource_advisor/service.py) | `Service.submit` 멱등성·attempt·outbox, `resolve_submission` |
| [src/resource_advisor/contracts.py](../../../src/resource_advisor/contracts.py) | `State`, `JobRequest` |
| [src/resource_advisor/store.py](../../../src/resource_advisor/store.py) | 테이블 4개, `claim`(SKIP LOCKED), `change_job`, `record_usage`, `job_route_filter` |
| [src/resource_advisor/worker.py](../../../src/resource_advisor/worker.py) | `Worker.submit_one`, `reconcile_submission`, `MLflowDelivery` |
| [src/resource_advisor/backends.py](../../../src/resource_advisor/backends.py) | `KubernetesBackend`, `SlurmBackend`, 예외 3종, `native_rejection` |
| [src/resource_advisor/configuration.py](../../../src/resource_advisor/configuration.py) | `worker_configuration` route 검증 |
| [src/resource_advisor/scheduling.py](../../../src/resource_advisor/scheduling.py) | `verify_adapter_plan` |
| [src/resource_advisor/inventory.py](../../../src/resource_advisor/inventory.py), [queue_inventory.py](../../../src/resource_advisor/queue_inventory.py) | K8s 수집, `signal`, `fresh_view` |
| [src/resource_advisor/slurm_inventory.py](../../../src/resource_advisor/slurm_inventory.py) | Slurm 수집, 응답 검증 |
| [src/resource_advisor/accounting.py](../../../src/resource_advisor/accounting.py) | `ledger_record`, `period_report` |
| [src/resource_advisor/artifacts.py](../../../src/resource_advisor/artifacts.py) | 아티팩트 전달 경로 |
| [examples/slurm_observer.py](../../../examples/slurm_observer.py) | 읽기 전용 forced command |
| [examples/slurm_executor.py](../../../examples/slurm_executor.py) | 실행·결과 forced command (`batch`, `scheduler_plan`, `containerized`) |

`db.py`는 이 저장소에 없다. 테이블 정의는 `store.py`에 있다.

## 기존 면접 자료

- [docs/nais-interview/START-HERE.md](../../nais-interview/START-HERE.md): 기존 원자료 진입점. 2026-10-07~08 실장비 기록 기반 자료는 history 수준이다.
