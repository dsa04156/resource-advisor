# 다중 학습 요청 관리 — 발표 자료

Resource Advisor로 학습 요청을 접수하고 Kubernetes/Kueue와 독립 Slurm VM에서 실행한 실증 자료입니다.

## 설명 이미지

| 파일 | 넣을 위치 | 설명 |
|---|---|---|
| [01-architecture.png](01-architecture.png) | 구성도 슬라이드 | Kubeflow 진입점, Resource Advisor, 두 실행 백엔드, 통합 관측과 결과 저장 |
| [02-submission-flow.png](02-submission-flow.png) | 작업 제출 과정 슬라이드 | 요청 → 검증·선택 → 네이티브 큐 → 학습 → 진행 확인 → 결과 확인 |

두 이미지는 16:9 설명도입니다. 실제 실행 화면이나 실험 측정값으로 제시하지 않습니다. 생성 방식은 built-in `image_gen`이며 [사용한 프롬프트](prompts.md)를 보관했습니다.

## 실제 화면과 실험 그림

화면은 실행 당시의 실제 콘솔을 1920×1080으로 캡처했습니다. 상태나 수치를 합성하지 않았습니다.

| 파일 | 보여주는 근거 |
|---|---|
| [03-kueue-pending.png](03-kueue-pending.png) | Kubernetes 학습의 큐 승인 대기 |
| [03a-kueue-condition.png](03a-kueue-condition.png) | `QuotaReserved=False`, CPU quota 부족 사유 |
| [03b-slurm-pending.png](03b-slurm-pending.png) | Slurm 작업 7의 `Resources` 대기 |
| [04-slurm-running.png](04-slurm-running.png) | Slurm 작업 6의 실제 epoch·loss·AllocTRES |
| [05-kubeflow-complete.png](05-kubeflow-complete.png) | KFP 실행 ID와 Kueue 완료 조건, 최종 학습 진행 |
| [06-slurm-history.png](06-slurm-history.png) | 동일 Slurm 작업의 대기 → 실행 → 결과 수집 → 완료 이력 |
| [07-mlflow-artifact.png](07-mlflow-artifact.png) | 완료 작업에서 연 MLflow 지표와 저장된 결과 아티팩트 |
| [08-native-queue-timeline.png](08-native-queue-timeline.png) | 공개 상태 기록에서 그린 타임라인 ([PDF](08-native-queue-timeline.pdf)) |
| [09-observed-training-loss.png](09-observed-training-loss.png) | 보관된 실제 진행 관측값 ([PDF](09-observed-training-loss.pdf)) |

그림 08·09는 [검증 기록](../../evidence/live-training-20261028.json)과 [진행 관측점](progress-samples.json)을 사용합니다. `render_results.py`로 재생성할 수 있습니다(`uv run --no-project --with matplotlib python render_results.py`). 타임라인은 워커 관측 기준이며 정확한 네이티브 실행 경계나 성능 비교가 아닙니다.

그림의 작업 식별자:

| 라벨 | 작업 ID | 워크로드 |
|---|---|---|
| K8s A | `j-59c965fa6aa04a909b7109bbce3571e4` | `live-train-k8s-a` |
| K8s B | `j-d4a5b35a77a24ac0830cef427f484da7` | `live-train-k8s-b` |
| Slurm A | `j-1122dca243744b70a10139ce064e99cc` | `live-train-vm-b` |
| Slurm B | `j-342c58fcb5e74b838ad70a288438c4d4` | `live-train-vm-a` |

## 실험 결과

- 학습 완료 11건: Kubernetes 7건, Slurm VM 4건. 이 중 Kubeflow 경유 완료 2건.
- 완료 11건 모두 초기/최종 checkpoint digest가 다르고 MLflow run·저장 아티팩트 digest가 있습니다.
- Kubernetes와 Slurm에서 실제 대기 → 실행 전이를 확인했습니다. 같은 요청 키 재시도는 같은 작업을 반환했습니다.
- 실행 중 취소 2건: Kubernetes·Slurm 각각 1건. 로그와 취소 이력을 보존했습니다.
- 초기 ServiceAccount 설정 오류 2건은 `SUBMISSION_UNKNOWN`으로 별도 보존했습니다. 성공 건수에 포함하지 않습니다.
- 기존 작업 863건·outbox 3866건의 digest, PVC 47개의 UID·spec은 유지됐습니다.
- 개발자 관련 테스트 306 통과·4 건너뜀. 전달 파일만 분리한 환경에서는 placement 테스트를 포함해 331 통과·4 건너뜀.

독립 코드 리뷰는 Codex의 변경된 후크 신뢰 확인에서 시작되지 못했습니다. 테스트와 실험 성공을 독립 리뷰 완료로 표현하지 않습니다.

## 슬라이드 구성 예시

1. **구현 구조** — 01번 이미지. “Resource Advisor가 두 실행 환경을 연결하고, 자원 배정은 각 네이티브 스케줄러가 담당합니다.”
2. **작업 제출** — 02번 이미지. “직접 제출과 Kubeflow 제출을 같은 작업 기록으로 추적합니다.”
3. **실제 대기와 진행** — 03a·03b·04번 중 필요한 캡처. “할당 대기 사유와 실행 중 학습 진행을 실제 관측값으로 표시합니다.”
4. **검증 결과** — 06·07·08·09번 중 선택. “대기→실행→완료와 모델 산출물·실험 기록 연결을 확인했습니다.”

## 발표에서 구분할 역할

- **Resource Advisor**: 요청 검증, 실행 대상 선택, 중복 요청 처리, 작업·관측·결과 기록.
- **Kubeflow Pipelines**: Resource Advisor를 호출하는 워크플로 진입점.
- **Kueue / Slurm**: 각 백엔드의 실제 큐와 자원 배정. Resource Advisor가 네이티브 스케줄러를 대체하지 않습니다.
- **통합 콘솔**: 백엔드에서 수집한 대기 사유, 작업 ID, 배치 위치, 할당 자원, 학습 진행과 결과를 표시합니다.

## 설명 문장

> 여러 학습 요청을 Resource Advisor로 제출하고, Kubernetes/Kueue와 별도 VM의 Slurm에서 실행하도록 연결했습니다. 통합 화면에서 실제 대기·실행 상태, epoch와 loss, 로그, 모델 산출물과 실험 기록까지 확인할 수 있습니다.

이번 검증은 합성 회귀 데이터에 대한 **CPU MLP 학습**입니다. Slurm은 QEMU TCG VM에서 실행되므로 서버 간 성능 비교로 해석하지 않습니다. 요청·예약 자원은 측정 사용률과 다르며, 학습 데이터의 점수는 일반화 성능을 뜻하지 않습니다.

구현 계약과 재실행 범위: [실시간 학습 관리](../../reference/live-training.md), [Slurm VM](../../reference/slurm-vm.md).
