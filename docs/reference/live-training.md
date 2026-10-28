# 실시간 다중 학습 관리

여러 학습 요청을 기존 Compute API·worker·콘솔로 접수하고, Kubernetes/Kueue와 Slurm VM의 실제 대기·할당·진행·결과를 함께 보여주는 기능입니다. 새 스케줄러나 별도 데모 앱은 없습니다.

## 학습 실행기

`resource_advisor.live_training`은 표준 라이브러리만 쓰는 CPU 학습기입니다. tanh MLP 하나를 미니배치 SGD와 명시적 역전파로 학습하며, sleep 없이 매 epoch 실제 가중치를 갱신합니다.

- epoch마다 `RA_PROGRESS` JSON 한 줄(job/attempt ID, epoch, loss, optimizer step, 경과, 관측 시각)을 flush합니다.
- 종료 시 기존 `RESOURCE_ADVISOR_RESULT` 한 줄에 결과, 학습 receipt, 출력 checkpoint(≤16KiB JSON)를 담습니다. 초기와 출력 checkpoint digest가 같으면 실패합니다.
- 데이터는 seed로 만든 **합성 회귀 데이터**입니다. `quality_value`는 학습 데이터 R²이며 일반화 성능이 아닙니다.
- GPU 실행이나 GPU fallback은 없습니다. capability와 variant는 `device_class=cpu`, `allocation_mode=cpu_only`, 가속기 0으로 등록합니다.

설정(seed·폭·학습률·epoch)이 다르면 workload·checkpoint·격리 binding을 따로 등록합니다(`live_training_registry.build`). 기존 10-step GPU 학습 fixture와 그 digest는 바꾸지 않았습니다.

## 학습 격리 계약

`TrainingIsolation`에 `device_class`(기본 `gpu`, 기본값은 서명에서 생략)와 `backend=slurm`을 추가했습니다. 기존 GPU binding의 서명은 그대로입니다.

| 경로 | 원본 보호 | receipt 주장 |
|---|---|---|
| Kubernetes | init container만 원본을 읽고 학습 컨테이너에는 사본만 read-only로 mount | `source_not_mounted=true` |
| Slurm (VM, native) | root 소유·쓰기 불가 원본을 실행기가 검증 후 시도별 0700 디렉터리로 복사 | `source_protection=operator-owned-readonly-host-copy` |

Slurm에서는 원본이 같은 호스트에 존재하므로 `source_not_mounted`를 주장하지 않습니다. receipt의 주장이 binding의 backend와 다르면 결과를 받지 않습니다. Slurm 학습은 GPU binding을 허용하지 않고, route의 `training_sources`(binding digest·원본 경로·작업 경로가 variant 명령과 일치)가 없으면 제출 전에 실패합니다. 다른 Slurm route의 학습은 계속 막힙니다.

## 관측

worker는 기존 상태 관측에 다음을 더해 job 기록에 저장합니다. 각 값은 관측 시각과 출처를 가집니다.

- Kubernetes(`observe_admission`): Job UID 라벨·소유자 UID·LocalQueue가 모두 맞는 Kueue Workload의 `QuotaReserved/Admitted/PodsReady/Evicted/Preempted/Finished` 조건과 ClusterQueue·flavor·resourceUsage. 이름만으로 매칭하지 않습니다. 읽기 실패는 `UNAVAILABLE`로 남기고 스케줄러 상태는 유지합니다. 권한은 별도 read-only Role(`deploy/live-training/rbac.yaml`)입니다.
- Kubernetes 배치: Job UID, Pod 이름·UID, 실제 `spec.nodeName`.
- Slurm(`observe_allocation`): 실행 중 squeue `%N` 노드와 같은 소유 Job ID의 sacct `AllocTRES`·Start. 기존 gateway의 고정 포맷 때문에 opt-in입니다.
- 진행(`observe_progress`): Kubernetes는 `kubectl logs --tail=60`, Slurm은 forced-command gateway의 `tail -c 16384`로 읽습니다. 이 시도의 ID와 일치하고 유한한 값만 기록(최근 20개)하며 로그 꼬리는 40줄·줄당 300자로 제한합니다. 읽기 실패는 `progress_error`로 남기며 상태를 바꾸지 않습니다.

예약 자원(Pod requests, AllocTRES)은 할당량이며 사용률이 아닙니다. CPU/GPU 사용률은 측정하지 않습니다.

## 콘솔

작업 상세의 요약 탭에 `실행 관측` 패널이 있습니다. Kubeflow 실행 ID(해당 시), 백엔드 작업·시도 ID, Kueue Workload·조건, Pod, 관측 노드, 예약 할당, 최근 epoch·loss 표, 로그 꼬리, 관측 실패를 보여줍니다. 실행 중 관측이 45초 넘게 갱신되지 않으면 지연으로 표시합니다. 목록은 기존 15초 자동 갱신을 사용합니다.

새 워크로드 마법사의 제출 단계에 `Kubeflow 파이프라인으로 실행`이 있습니다. 기존 `/research/pipelines` launch를 그대로 쓰며, 정책 기반 pipeline이 보존할 수 없는 저장 템플릿이나 직접 고른 대상(후보가 여럿인 경우)에는 표시하지 않습니다. 같은 요청의 재시도는 같은 키를, 새 요청은 새 키를 씁니다. Kubeflow launcher가 만든 작업의 상세에는 pipeline receipt의 run ID가 연결됩니다.

## 격리된 실행 배치

- `deploy/live-training/kueue.yaml`: cohort 없는 ClusterQueue `ra-live-training-cpu`(CPU 2·메모리 2Gi, etri-ser0001 flavor)와 LocalQueue `ra-live-cpu`. 2 CPU 학습은 한 번에 하나만 승인되어 다음 요청이 실제 Kueue 사유로 대기합니다. 다른 큐를 선점하지 않습니다.
- `deploy/live-training/worker.yaml`: 새 route(`ra-lab`, `live-k8s-cpu` 등)만 가진 worker. `coordinate_studies=false`, serving 없음, `scope_deliveries=true`로 artifact/MLflow 전달도 자기 route의 job만 claim합니다(기존 worker 기본값은 전역 claim 유지).
- Slurm VM gateway(`examples/slurm_executor.py`)는 운영자가 허용한 학습 binding digest, 자원 목록(`resources`), 최대 시간(`max_minutes`), 7열 squeue, 16KiB 진행 꼬리 읽기를 추가로 허용합니다. VM 쪽 runtime manifest는 `examples/live_training_vm_runtime.py`로 guest에서 만듭니다.
- 등록·제출·관측 기록 도구: `examples/live_training_acceptance.py`(토큰은 환경변수, 출력은 private 디렉터리).

## 범위와 한계

- GPU 학습이 아닙니다. 서버 GPU는 다른 프로젝트가 점유 중이어서 Kubernetes와 VM 모두 CPU 학습으로 검증합니다.
- VM은 사용자 공간 QEMU TCG(KVM 없음)라 CPU 성능 수치는 실제 서버와 비교할 수 없습니다.
- 진행 표시는 polling 기반입니다. 스트리밍, 체크포인트 재개, 자동 promotion은 없습니다.
- `SUBMISSION_UNKNOWN` 기록은 운영자 확인 전까지 재제출하지 않으며, 이를 해소하는 운영 도구는 이번 범위에 없습니다.

## 운영 runbook (lab)

구성 요소와 소유:

| 구성 | 형태 | 재시작 |
|---|---|---|
| API·콘솔 | 기존 `ra-api` Deployment(기존 host 포워드 서비스로 노출) | `kubectl -n resource-advisor-lab rollout restart deploy/ra-api` |
| Kubernetes 학습 worker | `ra-live-training-worker` (route `ra-lab/live-k8s-cpu`) | `deploy/live-training/worker.yaml`의 이미지 digest를 지정해 apply |
| Slurm VM worker | `ra-live-training-slurm-worker` (route `ra-lab/live-vm-slurm`, SSH client 이미지, forced-command 키만 mount) | `deploy/live-training/slurm-worker.yaml` |
| Slurm VM | QEMU TCG 프로세스, `deploy/slurm-vm/{start,stop,status}.sh` | `SLURM_VM_BIND=<host LAN IP> deploy/slurm-vm/start.sh` (worker Pod가 접속하려면 LAN bind 필요) |
| Kueue | `deploy/live-training/kueue.yaml` | 정적 객체 |

기존 `ra-worker`·`ra-slurm-worker`·inventory·restore·serving은 0으로 둡니다. 두 live worker는 `scope_deliveries=true`라 다른 route의 결과 전달을 가져가지 않습니다.

다시 실행할 수 있는 카탈로그(콘솔 새 워크로드 → 학습):

- `live-train-k8s-a`, `live-train-k8s-b` + 정책 `실시간 학습 · Kubernetes CPU`
- `live-train-vm-a`, `live-train-vm-b` + 정책 `실시간 학습 · Slurm VM CPU`

Kubernetes 쪽은 마법사의 `Kubeflow 파이프라인으로 실행`도 쓸 수 있습니다. KFP 실행에는 Kubeflow의 `admission-webhook-deployment`와 `katib-controller`(Pod mutating webhook, `failurePolicy: Fail`)가 Ready여야 합니다.

capability 수명: 등록한 capability는 운영자 readback으로 12시간(`valid_for_seconds=43200`) 유효합니다. 이 배포는 operational mode라 만료 뒤에도 제출은 계획되고 실제 승인은 Kueue/Slurm이 정합니다. 새 route를 관측하는 inventory collector는 없으므로 계획의 가용성은 `unknown (NO_FRESH_INVENTORY)`로 표시됩니다. capability와 workload는 불변이라 갱신하려면 새 ref로 다시 등록합니다(`examples/live_training_acceptance.py register`, VM은 `examples/live_training_vm_runtime.py`로 digest 확인).

VM gateway 변경 시: `/opt/ra/slurm_executor.py`와 `/etc/ra/slurm-executor.json`(controller), `/etc/ra/slurm-results.json`(results)은 root 소유이며 `ra`의 authorized_keys도 root 소유 파일입니다. 학습 binding이나 runtime을 바꾸면 controller 설정의 `training_bindings`·`native_bindings`를 함께 바꿔야 합니다.

## 검증 기록

2026-10-28 live 실행의 ID·전이·진행 관측 수·receipt digest·이력 보존 digest는 [live-training-20261028.json](../evidence/live-training-20261028.json)에 있습니다(주소·자격증명 제외). 요약:

- Kubernetes: 2 CPU quota로 한 요청이 실행되는 동안 다른 요청이 Kueue `QuotaReserved=False/Pending`으로 대기 후 실행됨(두 차례). 실행 중 요청마다 12–14회의 진행 관측.
- Slurm VM: 2 vCPU 노드에서 `PENDING(Resources)` → `RUNNING` 전이, 실행 중 AllocTRES cpu=2·mem=1G, 요청마다 14회 진행 관측.
- Kubeflow: launcher → Compute API → Kueue → 학습 완료(KFP 실행 2건). 이전에 Kubeflow webhook이 내려가 실패한 실행 2건도 기록.
- 같은 키 재시도는 같은 작업을 돌려주고 새 sbatch/Job을 만들지 않음. 취소는 Kubernetes(Job/Pod 삭제, quota 0)와 Slurm(sacct CANCELLED, 로그 꼬리 보존) 모두 확인.
- 이력 작업 863건·outbox 3866건의 digest는 실행 전후 동일.
- 첫 요청 2건은 worker manifest의 ServiceAccount 토큰 누락으로 `SUBMISSION_UNKNOWN`에 남아 있습니다. Job은 만들어지지 않았고 재제출하지 않으며, 이를 해소하는 운영 도구는 없습니다.
