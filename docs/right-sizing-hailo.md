# 새 Hailo 실행 피드백 검증

실험 `right-sizing-20261007-v1`의 W3는 기존에 검증한 ResNet50 HEF와
100개 입력을 그대로 사용한다. 모델·정밀도·품질·메모리·측정 경계가 GPU
W1/W2와 다르므로 **GPU/NPU 속도 비교는 NOT_COMPARABLE**이다.

새 qualification 한 번, 원본 `observe` 세 번, 측정 이력에 대한 추천과 명시적
승인, 독립 `fixed` 실행 한 번을 수행했다. 사용자 코드를 줄이거나 모델을
변환하지 않았다. API/worker 구현 commit은 `bb2f8342`; Hailo 실행 이미지와
runner는 기존에 검증한 digest를 재사용했다.

| 항목 | 새 실측 결과 |
|---|---:|
| Native Kubernetes/Kueue 작업 | 5 |
| API 작업 | 4 |
| Qualification 예약 | 5 NPU초 |
| API 작업 예약 | 9 NPU초 |
| 전체 예약 | 14 NPU초 / 14 CPU core초 |
| 관측한 프로토콜 wall time | 73.276937초 |
| 원본 관찰 3회의 평균 call time | 0.339591987초 |
| 독립 승인 실행 call time | 0.340695216초 |
| 실제 값과 근거 평균의 상대 차이 | +0.324869% |
| 독립 실행이 평균 구간 안에 포함됐는가 | 아니오 |

`recommendation_feedback`은 원본 profile ID, workload/context signature,
recommendation digest, native Job ID, usage attempt ID와 residual을 보존한다.
새 승인 실행의 receipt는 `COMPARABLE`, lifecycle은 `VERIFIED`다. 이는 독립
실행을 같은 근거와 비교했다는 뜻이며, 성능 향상이나 미래 성능 보장이 아니다.

S3·API·MLflow artifact bytes와 실행 결과를 실제로 다시 읽어 일치 여부를
확인했다. 정확도 80%, reference agreement 97%는 같은 고정 100개 이미지의
결과다. 네 번의 실행을 400개 독립 accuracy 표본으로 표현하지 않는다.
NPU utilization·NPU memory·power는 unknown이며 메모리 gate는 host RSS다.

첫 관찰 세 번의 lifecycle 표시는 기존 cold-start abstention 기록이 최신
profile보다 우선하는 projection 문제 때문에 `NEEDS_PROFILE`에 머물렀다.
별도의 새 추천 API는 측정 이력을 올바르게 사용했고 승인·실행은 통과했다.
이 표시 문제는 후속 수정 대상이며 원래 캡처는 보존한다.

원시 자료: [실행·품질·feedback](evidence/right-sizing-hailo-v1.json),
[비용과 원시 파일 hash](evidence/right-sizing-hailo-cost-v1.json),
[재계산한 감사 결과](evidence/right-sizing-hailo-audit-v1.json).
과거 fixture/image 준비 비용은 이번 프로토콜에서 새로 측정하지 않았다.
기존 자산을 재사용했으며 그 비용을 0으로 주장하지 않는다.

```bash
rtk proxy uv run python examples/audit_right_sizing_trial.py \
  docs/evidence/right-sizing-hailo-v1.json \
  docs/evidence/hailo-resnet50-inputs.json
rtk proxy uv run pytest -q tests/test_right_sizing_trial_audit.py
```

Auditor는 기존 Hailo model/quality/ownership/cost 검사를 재사용하고 새 feedback의
source chronology와 residual을 재계산한다. 테스트는 캡처를 읽고 digest, 비용,
중복, 미래 profile, residual, confirmation 재사용을 변조했을 때 거부하는지
확인한다. 새 하드웨어 실행을 테스트 fixture로 만들어 내지 않는다.
