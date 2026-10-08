# CPU·GPU 단계와 실제 계산 작업 연결

실제 CPU→GPU KFP 실행은 성공했지만 기존 콘솔은 단일 최상위 `run_key`만
찾아서 두 계산 작업을 표시하지 못했다. 이제 각 root task의 명시적 입력을
해석해 인증된 프로젝트의 Job을 연결한다. 두 단계는 각각 다른 Job/Attempt이며,
하나를 대표 작업으로 임의 지정하지 않는다.

## 실제 기록에서 확인한 것

| 보존된 실행 | 콘솔/API 연결 결과 |
|---|---|
| CPU→GPU 성공 `0bcd7480-8a4d-4759-b155-37b26b46ad17` | CPU와 GPU의 서로 다른 성공 Job 2개, 실제 CPU→GPU 의존관계 |
| 계산 제출 전 실패 `436b87e2-c0b7-4ad6-a26c-5bf9a442730f` | 연결 0개; 나중에 같은 key로 생성된 재시도 Job을 과거 실패에 붙이지 않음 |
| 기존 단일 launcher replay `6bc98218-b879-4722-8700-7789e09dc05d` | 기존 GPU Job 1개와 연결, 단일 `job_id` 호환 유지 |

KFP 상태와 계산 상태는 따로 표시한다. 단계에서 실제 계산 상세로 이동하면
기존 결과·MLflow·사용량을 조회한다. 동일 기록의 연결은 새 작업 제출을
뜻하지 않는다. 이 검증에서는 계산 작업을 추가 제출하지 않았다.

프로젝트가 설정되지 않은 계정의 live 조회는 HTTP 422로 거절됐다.
별도로 동일 key를 가진 다른 프로젝트 Job이 연결되지 않는 계약 시험을
통과했다. 전자를 설정된 KFP tenant 간 격리 시험으로 해석하지 않는다.

## 화면과 배포

그래프 높이를 350px에서 210px로 줄이고 KFP driver 기록을 접었다.
데스크톱에서 실제 두 단계와 작업 연결을 확인했다. 전체 상세는 내부 세로
스크롤을 사용하며 두 계산 버튼을 모두 스크롤 없이 볼 수 있다는 조건은
충족하지 않았다. 390px 모바일에서 페이지 가로 넘침은 없었고 GPU 버튼은
원래 GPU Job 상세를 열었다. 넓은 표는 안내와 함께 내부 가로 스크롤을 쓴다.
CPU/GPU 상세와 파이프라인 복귀는 직전 배포에서 검증했으며, 마지막 모바일
복귀의 즉시 조회는 비동기 로딩 전에 수행되어 성공 근거로 합산하지 않았다.

API source `75872c1c10320358f385f3f6619ab42474a2c281`의
[CI](https://github.com/dsa04156/resource-advisor/actions/runs/37505608639)가
성공했고 실제 배포 source hash, Ready, ArgoCD Synced/Healthy를 확인했다.
worker와 다른 서비스는 기존 image를 유지했다. 기존 entity 4,495개,
Job identity 594개, terminal Job·원장 각각 593개와 다른 정적 spec/UID,
Secret/PVC/쿼터를 보존했다.

[공개 증거](../evidence/pipeline-stage-links.json)는 실제 세 실행의 연결과
배포 근거를 담는다. 실제 계산·결과·비용은
[CPU→GPU 계산 보고서](cpu-gpu-pipeline-results.md)의 별도 시험을 따른다.

## 남은 범위

동적 task-output 표현식과 nested DAG는 추측해 연결하지 않는다. 계산 상태는
현재 저장된 상태이며 과거 특정 시점의 상태로 표시하지 않는다.
이 수정은 다단계 추적 기능이다. 데이터셋/모델 전달, 처음 요청한 네 종류
pipeline, Edge 재배치, HAMi 간섭, eBPF 연동이나 전체 HAIRP 완료의 근거는 아니다.
전체 범위는 [최초 설계 대조표](../hairp-original-scope-ko.md)를 따른다.
