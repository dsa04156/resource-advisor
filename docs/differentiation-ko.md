# 우리가 추가한 기능과 원래 설계의 완료 기준

이 프로젝트는 이기종 GPU·NPU의 **실행 가능성 검증 → 실제 관측 → 근거 있는
다음 실행 추천 → 승인 → 네이티브 실행 → 결과·비용 확인**을 연결한다.
이 저장소의 구현 기준은 독립 구현 설계 v0.3의 M0–M6이며, 공유 GPU 간섭
M7은 별도 확장이다. 처음 요청한 전체 HAIRP 구상에는 런타임 재배치,
HAMi 간섭 모델, eBPF 연동도 들어 있다. v0.3의 완료를 전체 HAIRP 완료로
바꾸어 말하지 않는다. 두 범위의 차이와 남은 연결은
[최초 HAIRP 설계 대조표](hairp-original-scope-ko.md)에 기록한다.
[쉬운 구성요소 안내](platform-explained-ko.md)에서는 각 도구의 역할과 직접
만든 연결을 연구자의 작업 흐름으로 설명한다.

## 기존 기술과 직접 만든 부분

| 기존 기술의 역할 | 직접 개발한 연결과 판단 | 현재 근거 |
|---|---|---|
| Kubernetes/Kueue와 Slurm은 선언된 요청, 큐, 쿼터, 우선순위에 따라 입장·배치한다 | 공통 SchedulingProfile을 실제 backend의 Queue/Flavor/Priority 또는 Account/QOS/Partition으로 변환하고, 제출 시 계획과 실제 설정의 일치를 검사한다 | [공통 정책과 자동 제출](scheduling-profiles.md), [실제 네이티브 실험](native-scheduler-lab.md) |
| 장치 플러그인은 자원 등록·예약을 제공한다 | 코드·이미지 아키텍처·드라이버·변환 모델·입력·정확도에 맞는 RuntimeVariant를 검증한다 | [GPU 계약 경계](contract-boundaries.md), [Hailo ResNet50](hailo-resnet50.md), [Slurm Orin 실행](slurm-api-results.md) |
| MLflow는 실험의 파라미터·측정값·파일을 저장한다 | 작업 서명과 실행환경 서명을 구분하고, 결과 digest·attempt·품질을 검증한 뒤 추천 근거와 승인 범위를 연결한다 | [서명과 설계 결정](architecture.md), [이력 동결](lookup-history.md), [실측 추천 데모](approved-gpu-demo.md) |
| 모니터링은 측정값과 상태를 수집한다 | 예약량과 실사용을 구분하고, 추천 보류·대기 원인·작업과 노드·실험 시점의 상태를 한 화면에서 추적한다 | [운영 콘솔](research-console.md), [노드 상태 재생](native-scheduler-lab.md) |
| Kubeflow는 워크플로 단계를 실행한다 | 동일 ComputeJob/Attempt에 KFP·네이티브 작업·MLflow·결과·사용량 ID를 연결하고 중복 제출과 취소 후 정리를 처리한다 | [KFP 실제 실행](kubeflow-pipeline.md), [실패 기록 복구](failure-tracking.md), [사용량 원장](accounting.md) |
| 기본 자원 요청은 연구자가 정한다 | 처음 보는 작업은 관측하거나 보류하고, 동의·예산 안에서 lookup/random/qLogNEI로 후보를 시험한 뒤 독립 최종 확인을 거친다 | [원래 계약의 음성 시험](contract-boundaries.md), [같은 예산 정책 비교](policy-comparison-v2.md) |

자동 제출 정책과 성능 추천은 구분한다. 현재 공통 정책의 후보 선택은
호환성·현재 예약 여유·backend 선호를 사용하는 결정 규칙이다. 가장 빠른
장치를 예측하는 알고리즘이라고 부르지 않는다. 성능 추천은 비교 가능한
실측 프로파일, 품질·불확실성·예산 조건을 거치는 별도 과정이다.

백필·갱 스케줄링·큐 우선순위 자체도 기존 스케줄러의 기능이다.
우리의 개발 범위는 그 설정을 공통 정책으로 연결하고, 실제 입장·예약·실행
결과와 대기 원인을 수집해 실험 화면에서 비교·재생하는 것이다. 특히
Slurm의 시간 분할 gang scheduling과 여러 Pod의 동시 입장은 의미가 다르다.
실험별로 사용한 네이티브 기능과 검증 범위를 명시한다.

기존 도구의 역할은 공식 문서에서도 확인할 수 있다:
[Kueue의 입장과 Pod 스케줄링 구분](https://kueue.sigs.k8s.io/docs/overview/),
[Slurm의 백필](https://slurm.schedmd.com/sched_config.html),
[Slurm gang scheduling](https://slurm.schedmd.com/gang_scheduling.html),
[Kubeflow 파이프라인](https://www.kubeflow.org/docs/components/pipelines/concepts/pipeline/),
[MLflow 실험 추적](https://mlflow.org/docs/latest/tracking).

## 연구자가 사용하는 흐름

1. Notebook·콘솔·KFP에서 작업과 요구 자원을 정의한다. 기본 제출은 GPU를
   먼저 고르지 않고 등록된 작업과 공통 SchedulingProfile을 선택한다.
2. API가 검증된 실행환경 후보와 현재 자원 정보를 확인한다. CUDA 작업을
   NPU에 임의 변환하지 않으며, 처음 보는 모델은 실행환경 검증이 필요하다.
3. 공통 정책을 해당 backend의 실제 Queue/Flavor/Priority 또는
   Account/QOS/Partition으로 변환해 제출한다. 입장과 할당은 Kueue·Slurm이
   결정한다. 자리가 없으면 실제 네이티브 큐에서 기다린다.
4. 콘솔에서 대기 → 할당 → 실행 → 결과와 그 시점의 노드 상태를 확인한다.
   실시간 관측과 기록 재생은 구분하며, 측정하지 못한 값은 알 수 없음으로
   표시한다.
5. 결과 검증 후 동일 attempt의 결과 파일·MLflow run·사용량을 연결한다.
   실패·취소 작업의 예약 시간도 보존한다.
6. 성능을 개선하려면 동의와 측정 예산을 정해 비교 가능한 관측을 모은다.
   추천 근거를 동결하고 승인한 뒤 별도 최종 실행으로 확인한다.

이 흐름의 개발 난점은 각각의 ID·상태·권한·재시도 의미가 다른 시스템을
연결하면서, 큐 제출을 실행 완료로 오인하거나 응답 유실 후 작업을 중복
제출하지 않게 만드는 것이다. 따라서 화면뿐 아니라 결과 서명, 권한 검사,
상태 머신, outbox와 사용량 원장이 직접 만든 핵심 구성요소다.

## 성과로 말할 수 있는 것

- Kubernetes/Kueue GPU, Hailo NPU, Slurm Orin GPU의 실제 실행과 결과 연결.
- 작업·실행환경 식별, 품질 검사, 보류·승인, 멱등성, 원장과 실험 기록.
- 실제 qLogNEI 후보 선택과 최종 확인, 동일 예산 기준선 비교.
- 실패한 모델 검증과 불확실한 추천도 남기며, 추가 측정 비용까지 보고.
- 큐·할당·실행 흐름과 그 시점의 노드 상태를 재생하는 운영 화면.

성능 우수성은 별도 실험 주장이다. [정책 비교](policy-comparison-v2.md)에서는
BO의 선택 우위가 확인되지 않았다. [운영 비교](operational-comparison.md)에서도
작은 계산시간 차이만으로 프로파일링 초기 비용을 회수했다고 볼 수 없었다.
이는 비용을 누락한 절감률을 만들어내지 않고 실제 효과를 검증한 결과다.
실행환경 검증의 [실제 Hailo 변환물 시험](hailo-artifact.md)에서는 정상
ResNet50은 통과하고 다른 ResNet18 HEF는 결과 생성 전에 거절됐다.
큐 입장·장치 등록과 모델 실행 가능성을 별도로 검증한 근거다.
또한 [실행환경 연결 수정](kubernetes-runtime-bundles.md)은 새 작업 정의에서
승인된 런타임 mount가 빠지던 실제 실패를 해결했다. 새 variant가 기존
승인 bundle을 명시적으로 참조하고, GPU 실행·결과·MLflow·사용량까지
같은 attempt로 확인했다. 새 알고리즘의 성능 향상과는 별개의 운영 기능이다.

기존 기술을 연결한 기능과 성능 우위는 따로 평가한다. 예를 들어 이 프로젝트의
차별점은 “BO를 설치했다”가 아니라 **동의와 비용 상한 안에서 실제 후보를 실행하고,
결과의 품질·환경·근거를 검사한 뒤 독립 확인과 승인으로 연결한 흐름**이다.
이 흐름은 동작했지만 기존 정책보다 더 빨랐다는 결론은 아직 없다.

## 원래 설계의 완료 판정

[전체 수용시험 표](goal-audit.md)와 [M0–M6 구현 원장](implementation.md)이
완료 여부의 기준이다. 화면이나 라이브러리 설치만으로 완료를 판단하지 않는다.
[E0–E7 대조표](scenario-acceptance.md)에 실험별 근거와 미완료 경계를 연결했다.

현재 실행·추천·기록의 핵심 경로는 실장비 근거가 있다. 남은 필수 범위에는
사용자 코드를 변경하지 않고 노드·프로세스·장치 지표를 수집하는 관측 전용 경로가
포함된다. 현재 `observe` 실행은 정해진 프로그램의 결과 계약을 사용하므로,
임의 연구 코드의 관측 전용 기능까지 완료한 것으로 표시하지 않는다. 미수집한
학습 step·처리량·정확도는 unknown으로 유지하고 추천용 정상 프로파일과 구분해야 한다.
이 항목은 장비 복구를 기다리지 않고 구현할 수 있는 작업이다.

[Slurm 두 프로젝트 시험](slurm-project-isolation.md)은 별도 계정·소유자,
8건의 실제 제한 거절과 양방향 고우선순위 선행을 확인했다. 성공 6건의
결과·원장·MLflow는 검증했지만 마지막 작업은 연결 장애 후 취소 요청
상태여서 종료·비용과 남은 호환성·장애 수용시험이 남아 있다.
[Slurm 접수 응답 유실·worker SIGKILL 복구](slurm-response-recovery.md)는 동일
작업·결과·원장·MLflow 복구가 확인됐다. 수정한 공개 시험 도구로 실행한
네 번째 시험은 제출 호출 한 번까지 보존해 사전 복구 절차를 통과했다.
앞선 시험 오류와 비용도 함께 남겨 두었다.
Slurm Pi의 NPU는 실제 PCIe 탐지와 모델 실행 근거가
없으므로 미지원 상태를 유지한다. 이 장치의 가속기 실행은 소프트웨어만으로
완료했다고 선언할 수 없다. 장비별 지원표와 실패 근거를 함께 남겨야 한다.

2026-10-07 읽기 전용 재확인에서도 같은 Slurm 작업은 `CANCEL_REQUESTED`이며
네이티브 종료·원장 비용을 조회할 수 없었다. Pi는 접속 가능하고 CPU는 보이지만,
PCIe 링크가 내려가 DEEPX/Hailo 가속기 endpoint가 탐지되지 않았다. 기존 작업을
다시 제출하거나 상태를 성공으로 바꾸지 않았다. 한편 KFP의 CPU-only launcher는
워크플로 제어용이며, 원본의 별도 CPU 계산 Job 결과 검증을 대신하는 근거가 아니다.

현재 스키마의 [독립 DB 복구](restore-rehearsal.md)는 7개 테이블의 25,771개
레코드, 결과 파일 567개, MLflow·사용량 연결 582건, 기록된 스케줄링 실험
18개를 대상으로 통과했다. 이 결과는 메타데이터와 기존 외부 결과의 연결
복구 근거이며, 오브젝트 저장소나 MLflow 자체를 복구했다는 뜻은 아니다.

기존 Edge 런타임의 실행 중 이동·재계획은 독립 v0.3 프로젝트의 책임 범위가
아니다. 현 서비스는 제출 전 추천과 실행 기록 연결을 제공한다. 대규모 HPC,
모든 NPU의 실행, GPU/NPU 혼합 분산학습 또는 전역 단일 스케줄러를 구현한
것으로 설명하지 않는다. 전체 완료는 아직 증명되지 않았다.
