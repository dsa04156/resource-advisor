# 최초 HAIRP 설계와 현재 구현의 차이

**전체 HAIRP 완성은 아직 아니다.** 현재 실장비 근거가 있는 것은 독립
Resource Advisor의 작업 검증·제출·실행·결과·추천·운영 경로다.
처음 요청한 HAIRP의 Edge 런타임 확장 전체와 이후 독립 구현 v0.3은
구분해서 평가한다. 후자의 범위를 전자의 완료 기준으로 대체하지 않는다.

기존 KubeEdge 런타임의 Capture/Preprocess/Inference, Workflow Manager,
State Aggregator, Placement Engine과 Keep/Migrate/Offload는 원래 시스템에
남아 있다. 공개 프로젝트에서 이를 다시 만들거나 현재 추천 API를 그
런타임의 재배치 엔진으로 표시하지 않는다. 기존 기능을 보존하는 것과
새 플랫폼에 연결해 실험으로 검증하는 것은 각각 별도의 완료 조건이다.

## 기존 기술 위에 직접 만든 것

Kueue는 Kubernetes 작업의 쿼터와 입장을 관리하며 Pod의 노드 배치는
Kubernetes에 맡긴다([공식 책임 구분](https://kueue.sigs.k8s.io/docs/overview/)).
Slurm도 자체 네이티브 큐와 배치 정책을 유지한다
([스케줄링 설정](https://slurm.schedmd.com/sched_config.html)).
MLflow는 실험 파라미터·측정값·결과 파일을 기록한다
([실험 추적](https://mlflow.org/docs/latest/ml/tracking/)).
이를 사용했다는 사실만으로 새로운 스케줄링 알고리즘을 만들었다고 하지 않는다.

| 직접 만든 기능 | 해결하는 문제 | 실제 근거 |
|---|---|---|
| 공통 작업·SchedulingProfile 계약과 backend별 변환 | 연구자가 GPU를 먼저 지정하지 않고 작업 조건과 정책으로 제출한다. 계획과 실제 Queue/QOS 설정이 달라지면 거절한다 | [정책 API와 자동 제출](scheduling-profiles.md) |
| 작업 서명·환경 서명·장치별 RuntimeVariant·결과 검사 | 장치 등록만으로 실행 가능하다고 판단하거나 서로 다른 모델/환경 결과를 섞는 문제를 막는다 | [계약 경계](contract-boundaries.md), [Hailo 실제 모델](hailo-resnet50.md), [Slurm Orin](slurm-api-results.md) |
| 실측 이력 기반 추천·동의·예산·승인·독립 확인 | 처음 보는 작업에 성능을 지어내지 않고, 허용된 측정 비용 안에서 다음 실행 설정을 검토한다 | [추천 데모](approved-gpu-demo.md), [같은 예산 비교](policy-comparison-v2.md) |
| 멱등 제출·상태 복구·결과/MLflow/원장 연결 | 제출 응답 유실 후 중복 GPU 작업이 생기거나 실패·취소 비용이 사라지는 문제를 처리한다 | [Slurm 복구](slurm-response-recovery.md), [사용량](accounting.md), [DB 복구](restore-rehearsal.md) |
| 운영 콘솔·기록 재생·실험 시점의 노드 상태 | 대기 이유, 할당 대상, 실제 실행 상태와 그때 관측된 자원을 함께 추적한다 | [운영 콘솔](research-console.md), [실장비 시나리오](native-scheduler-lab.md) |

이는 이 프로젝트에서 개발한 통합 기능과 검증 범위다. 상용 플랫폼에도
유사한 기능이 있을 수 있으므로 세계 최초나 기존 제품에 없는 기능이라고
주장하지 않는다. 백필·갱 입장·토폴로지 배치의 네이티브 동작은 기존
스케줄러의 기능이며, 플랫폼은 정책 연결과 관측·재생을 담당한다.

## 처음 요청한 범위의 대조표

아래 번호는 최초 HAIRP 요청의 항목 번호다. “일부 구현”은 설치나 코드
존재만으로 전체 수용시험을 통과했다는 뜻이 아니다. 세부 증거는
[현재 수용시험 표](goal-audit.md)와 각 보고서를 따른다.

| 최초 항목 | 현재 상태 | 전체 HAIRP 완료에 필요한 추가 근거 |
|---|---|---|
| 1–2 책임 분리·전체 제어 흐름 | Kubernetes와 Slurm을 분리했고 KFP/API/실행/결과 경로가 있다. 기존 Edge 런타임은 보존 | 공통 연구 작업과 기존 stage 런타임의 버전 있는 연결 계약 및 실제 stage 실행·재계획 연결 |
| 3–4 공통 작업 모델·라우터 | ComputeJob/Attempt/RuntimeVariant/SchedulingProfile과 네이티브 adapter 구현 | 최초 ResearchJob 요구의 SLA/dependency/다중 노드 HPC 의미를 현재 계약과 대조하고 실제 지원 여부 검증 |
| 5 Kueue | 두 프로젝트 큐·쿼터·우선순위·실제 GPU 대기/입장 검증 | 예시 4/8 GPU 쿼터는 현재 장비 용량과 분리해 재현 가능한 lab 설정으로 제공; 대규모 공정성은 미검증 |
| 6 Slurm | 실제 Orin GPU 제출·조회·취소·결과·QOS·응답 유실 복구 검증 | [두 프로젝트 시험](slurm-project-isolation.md)의 제한·접근 차단·양방향 선행은 관측; 연결 장애 후 마지막 작업 종료·비용 및 전체 수용시험은 미완료 |
| 7 Kubeflow | 실제 launcher 파이프라인과 콘솔 제출/상태 연결 | 최초 Benchmark/Training/Edge Deployment/Runtime Evaluation 네 종류 전체를 실제 용도로 실행·연결 |
| 8 MLflow | 실제 실행 metadata·측정·artifact 및 실패/취소 tracking | 모든 요구 metric/환경 metadata의 장치별 수집, 모델 버전 lifecycle 및 전체 pipeline artifact 범위 |
| 9–10 하드웨어/작업 프로파일·장치 탐지 | 정적 capability와 동적 inventory를 분리하고 검증된 GPU/Hailo/Orin 계약 제공 | 최초 공통 HardwareProfile/WorkloadProfile 대응, AMD 및 vendor NPU plugin의 지원표와 실제 장비별 검증 |
| 11–12 HAMi·간섭 모델 | 공유 슬롯과 물리 장치를 구분; M7 확장은 현재 미완료 | 실제 HAMi 논리 할당과 물리 사용량의 동시 관측, 동시 실행 slowdown, A/B/C 비교 |
| 13–14 stage 프로파일·benchmark | 실제 CUDA matmul/CNN 및 별도 Hailo 모델 runner와 JSON 결과 | Capture/Preprocess/Inference/Postprocess 이력, 동일 workload의 장치 간 비교, 작은 transformer와 CSV/energy 수집 |
| 15–16 Placement Engine v2·설명 | 제출 전 호환성/여유 기반 선택 이유와 성능 추천 근거 제공 | 기존 runtime의 실제 Keep/Migrate/Offload/Reject 결정에 quota/interference/network/benchmark를 연결하고 migration 비용 측정 |
| 17–18 Prometheus·Grafana | 실제 telemetry 수집과 별도 운영 콘솔 구현 | 최초 열 종류 Grafana dashboard 및 KFP/MLflow/placement service metric 전체 연결; 콘솔만으로 대체 완료 처리하지 않음 |
| 19 accounting | 모든 terminal outcome의 원장, 물리/가상 단위와 unknown 구분 | 준비·utilization·energy·연결 끊긴 노드의 완전한 비용 근거 |
| 20–22 Ansible·Terraform·ArgoCD | scoped lab telemetry Ansible와 정적 서비스 GitOps의 실제 근거 있음 | 최초 provisioning 역할 전체는 미완료; Terraform은 실제 VM/network/storage 생성 대상이 있을 때 적용하며 빈 wrapper는 추가하지 않음 |
| 23 eBPF | 현재 Resource Advisor에서 직접 검증된 통합 근거 없음 | RTT/retransmission/socket/queue 지표 → State Aggregator, Prometheus-only 대비 실제 비교 |
| 24 장애 처리 | 취소·응답 유실·worker crash·MLflow 장애·result 실패·복구의 일부 실험 통과 | Xid/node loss/edge disconnect/Slurm DOWN 등 최초 실패 목록의 장비별 수용시험 |
| 25–26 실험·평가 | 실제 정책/운영 비교, 실패 비용·null improvement까지 보고 | Static/기존 replanning/quota/interference/benchmark/full 정책의 동일 조건 비교 및 전체 metric |
| 27–28 저장소·공개 안전 | 독립 공개 코드와 site config 분리 | 다른 저장소의 구조/설치 흔적을 live integration 근거로 합산하지 않음; 공개 증거에서 비밀·내부 정보 제외 |
| 29–30 순서·호환성 | 기존 런타임/cluster 버전 보존, 자원별 qualified 계약 | 새 runtime 연결과 추가 component도 현재 버전에 대한 독립 호환성 검사 필요 |
| 31 최종 시연·시니어 리뷰 | 실제 GPU/NPU/Slurm/KFP 실행과 다수 보고서, 검토 문서 제공 | 최초 전체 구성요소와 위 미완료 항목의 시연/근거 연결 후 최종 완료 판정 |

현재 독립 구현 v0.3의 필수 M0–M6도 전체 완료로 선언하지 않았다.
Slurm 두 프로젝트의 계정·제한·접근 차단과 양방향 선행은 실제로
관측했다. [해당 시험](slurm-project-isolation.md)은 마지막 작업에서
연결이 끊겨 부분 완료이며, 같은 작업의 종료·비용과 남은 호환성·장애
시험이 남아 있다. [E0–E7 대조표](scenario-acceptance.md)는 작성했다.
Pi의 DEEPX NPU는 PCIe 장치 탐지가 확인되지 않아 실행 후보로
등록할 수 없다. 다른 Hailo에서 성공한 모델을 그 장치의 근거로 재사용하지 않는다.

## 성과를 설명하는 문장

“기존 Kubernetes/Kueue와 Slurm의 스케줄링 책임을 유지하면서, 연구 작업의
공통 정책·이기종 실행환경 검증·실측 추천과 승인을 연결했습니다. 실제
GPU와 NPU 작업의 대기·실행·결과·MLflow·사용량을 한 콘솔에서 추적하고,
응답 유실과 취소·메타데이터 복구를 실장비로 확인했습니다.”

이 문장은 현재 증거에 맞는 설명이다. “모든 장치 자동 최적 배치”,
“GPU 간섭 최적화 완료”, “실행 중 무중단 migration”, “BO가 더 우수”는
현재 성과로 주장하지 않는다. 전체 HAIRP 완료는 위 추가 근거가 모인 뒤에 판정한다.
