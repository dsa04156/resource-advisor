# 코드 변경 없는 실제 CUDA 작업 관측

2026-10-07, [미리 고정한 계획](passive-gpu-observation-results.md)의 **실제
Kubernetes GPU Job 한 개**로 관측 경로를 확인했다. 일반 PyTorch 프로그램에
Resource Advisor import나 계측 hook을 추가하지 않았다. 전체 플랫폼 완료나
성능 비교의 근거는 아니다.

| 확인 항목 | 실제 결과 |
|---|---|
| 실행 | RTX 5080, 기존 Kueue 입장, GPU 1개·CPU 1개·2 GiB |
| 코드 | 실행 전·관측 중·관측 후 같은 SHA-256; 읽기 전용 mount |
| 관측 | 동일 Pod/PID 1에서 12초 수집, 1초 간격, 프로세스·노드·물리 GPU 12개 표본 |
| 생명주기 | 수집 종료 시 프로그램 Running; 이후 정상 출력·exit 0 |
| 물리 GPU | CUDA가 출력한 장치 UUID와 NVML UUID digest 일치 |
| 저장·권한 | 운영자 등록, 연구자 조회, 동일 결과 재등록 시 같은 digest; 연구자 등록 403·다른 프로젝트 조회 404 |
| 기존 상태 | 4,476 기존 entity, 592 작업 식별자, 591 terminal Job·591 원장 유지; 서비스·Secret·PVC·쿼터 유지 |
| 비용·정리 | 노드 배정~종료 **41초**(준비 5초·컨테이너 36초); 시험 Job/ConfigMap만 삭제, 큐 반환 확인 |
| 화면 | 실제 기록의 12개 표본·미수집 문구 확인; 1366×768·390×844 시각 점검 |

원본 코드와 식별자는 [공개 프로그램](../../examples/passive-gpu/program.py),
실측 표본과 검증 결과는 [공개 결과 JSON](../evidence/passive-gpu-observation-result-v1.json)에
있다. 원본 native 객체·장치 UUID·서버 정보·스크린샷은 비공개 운영 증거로
보관하고 공개 문서에는 넣지 않았다. 계획 동결 commit `81e56f8`의 CI
`37494786627`도 통과했다.

GPU 전체 사용률은 이 시험에서 1%, 전체 장치 메모리는 약 840 MiB,
온도는 33–35°C, 전력은 약 42 W였다. 프로그램은 각 연산 뒤 잠깐 대기한다.
이 결과로 고부하 성능이나 작업별 GPU 사용률을 주장하지 않는다.
프로세스 표본 최대 RSS는 약 751 MiB이며 전체 실행의 메모리 peak가 아니다.
요청한 수집 창은 12초지만 첫~마지막 표본 구간은 약 11.025초다.
센서 조회 시간 합계 약 0.023초도 전체 관측 overhead의 인과적 추정값이 아니다.

정확도·학습 step·처리량·프로세스 네트워크는 **미수집**이다. 관측 등록은
ComputeJob, 모델 프로파일, MLflow 실행 run 또는 사용량 원장을 만들지 않는다.
노드 배정~컨테이너 종료 41초는 요청 자원의 예약 비용 대용 구간이며, 실제
GPU 활성 시간은 아니다. Kueue QuotaReserved~실제 해제 전이 시각은 보존하지
못했다. 따라서 쿼터 예약 시간은 종료 시각부터 최종 반환 확인까지의
하한·상한으로 JSON에 남겼고, 정확한 값은 null이다. 수집 시간이나
컨테이너 36초로 전체 예약 비용을 대체하지 않았다.

## 재현 방법과 경계

1. [native Job 예시](../../examples/passive-gpu/job.example.yaml)의 image를 검증된
   digest로, namespace/queue/PVC/node를 허용된 lab 값으로 대체한다. 기존
   쿼터와 드라이버를 변경하지 않는다. 위 프로그램을 immutable ConfigMap에
   넣고 읽기 전용으로 mount한다. 기존 source override mount를 복사해 새
   collector 모듈을 가리지 않도록 한다.
2. Job UID, Kueue Admitted 조건, 한 개 Pod의 GPU request와 배정 노드를
   확인한다. `ready` 출력 뒤 프로그램이 실행 중일 때 PID 1을 관측한다.
   binding에는 실제 project/cluster/node/native UID/attempt를 넣는다.
3. [수집 CLI](passive-observation.md)의 `--duration 12 --interval 1`과
   실제 CUDA UUID의 `--gpu-uuid`를 사용한다. 수집기는 같은 컨테이너에서
   `/proc`와 NVML만 읽는다. 출력은 별도 관측 파일이며 원래 프로그램을
   수정·재시작·종료하지 않는다. 자동 실행 시 준비·대기는 120초 이내로
   제한하고 실패하면 같은 시험을 대체 제출하지 않는다.
4. 수집 종료와 프로그램 종료를 따로 확인하고 원래 코드 hash·정상 출력·
   실제 컨테이너 시작/종료 시간을 보존한다. 관측 파일은 기존
   `examples/import_observation.py`로 운영자 등록하고 연구자 API/Jobs 화면에서
   확인한다. 보존 검사는 import 전후 DB 기록과 static spec/UID/쿼터를 비교한다.
5. 원본 terminal 증거와 실패 비용까지 보관한 뒤 자신이 만든 Job/ConfigMap만
   정리하고 큐 예약이 해제됐는지 확인한다. 이 문서의 예시를 기존 시스템에
   검증 없이 그대로 적용하지 않는다.

외부 프로세스와 native 실행의 연결은 운영자의 확인이며 원격 attestation이
아니다. Slurm·Jetson/NPU 관측, 임의 프로그램 전체, 연속 수집의 수용시험은
이 한 번의 CUDA 결과로 대신하지 않는다. 독립 v0.3의 관측 경로에 대한
이 fixture는 통과했지만 [전체 수용시험](../right-sizing-claim-audit.md)과
[최초 HAIRP 범위](../hairp-original-scope-ko.md)는 계속 열려 있다.
