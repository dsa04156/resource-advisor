# 우리가 추가한 기능과 원래 설계의 완료 기준

이 프로젝트는 이기종 GPU·NPU의 **실행 가능성 검증 → 실제 관측 → 근거 있는
다음 실행 추천 → 승인 → 네이티브 실행 → 결과·비용 확인**을 연결한다.
기준은 독립 구현 설계 v0.3의 M0–M6이며, 공유 GPU 간섭 M7은 별도 확장이다.

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

## 원래 설계의 완료 판정

[전체 수용시험 표](goal-audit.md)와 [M0–M6 구현 원장](implementation.md)이
완료 여부의 기준이다. 화면이나 라이브러리 설치만으로 완료를 판단하지 않는다.

현재 실행·추천·기록의 핵심 경로는 실장비 근거가 있다. 남은 필수 범위에는
Slurm 두 프로젝트의 공동사용·응답 유실/worker 장애 복구 시험과 전체 E0–E7
대조표 정리가 있다. Slurm Pi의 NPU는 실제 PCIe 탐지와 모델 실행 근거가
없으므로 미지원 상태를 유지한다. 이 장치의 가속기 실행은 소프트웨어만으로
완료했다고 선언할 수 없다. 장비별 지원표와 실패 근거를 함께 남겨야 한다.

기존 Edge 런타임의 실행 중 이동·재계획은 독립 v0.3 프로젝트의 책임 범위가
아니다. 현 서비스는 제출 전 추천과 실행 기록 연결을 제공한다. 대규모 HPC,
모든 NPU의 실행, GPU/NPU 혼합 분산학습 또는 전역 단일 스케줄러를 구현한
것으로 설명하지 않는다. 전체 완료는 아직 증명되지 않았다.
