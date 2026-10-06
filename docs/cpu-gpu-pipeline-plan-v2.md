# M1 CPU → GPU 계산 v2 — 계약 준비 오류 수정 후 별도 시험

[v1 계획](cpu-gpu-pipeline-plan.md)은 qualification 계약 검증에서 실패했다.
CPU·GPU 계산 자체는 정상 종료했지만 전체 QualityPolicy 대신 일부 필드만
서명했다. 이 결과를 성공으로 바꾸거나 API 프로파일로 등록하지 않았다.
[실패·비용 보고](evidence/cpu-gpu-m1-v1-failure.json)에 두 native Job,
GPU 예약 대용 구간 2초·CPU core 예약 대용 구간 11초를 보존했다.
v1에는 KFP run/API Job이 없고 소유한 qualification Job만 정리했다.

이 문서는 수정 후 **별도 v2 시험을 실행하기 전** 고정한 계획이다.
v1 결과를 재작성하지 않는다. v1과 v2의 비용은 최종 보고에서 함께 계산한다.
v2 안에서는 자동 retry·대체 제출 없이 최대 계산 Job4개만 실행한다.

수정 사항은 준비 코드의 canonical 계약 구성뿐이다. `QualityPolicy` 전체를
모델로 검증하고 그 `signature`를 identity에 넣는다. 제출 전에 CPU/GPU
각각 `WorkloadSpec`, `RuntimeVariant`, `Candidate`, `CapabilitySnapshot`을
검증하고 serialized form을 다시 검증한다. 두 prototype, native manifest,
계약 digest를 고정한 뒤 native 제출을 허용한다. 최초 준비 단계의 잘못된
result envelope 타입 가정도 고쳤으며 동일 원본 Job을 읽어 실패를 보존했다.

행렬·정밀도·반복·수치 검증·자원량·쿼터·허용 노드·실행/대기/수집 상한·
owner lease·최종 API/MLflow/S3/원장 확인은 v1 계획 그대로다. 실패를
통과시키기 위해 품질 기준이나 메모리 기준을 낮추지 않는다. worker는 이미
검증·배포한 source `b8d24f5`와 같은 image/lock을 유지하며 추가 rollout은 없다.
새 qualification을 실제로 수행한 뒤 새 capability/variant/workload ref로
등록한다. 이전 capability의 시각이나 v1의 잘못된 identity를 수정해 쓰지 않는다.

v2 통과 여부와 두 시험의 전체 비용은 실행 후 별도 결과에 기록한다.
설치·검사 통과만으로 M1 완료를 선언하지 않는다.
