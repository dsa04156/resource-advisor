# Profile-Guided Heterogeneous Resource Right-Sizing

상태: **필수 gate 감사 진행 중**. 현재 Slurm controller 연결이 막혀 있으며,
GPU/NPU의 동일 logical workload 성능 비교는 성립하지 않는다. 전체 완료,
일반적인 최적화 성공, 모든 이기종 가속기의 성능 예측을 주장하지 않는다.

## 1. Problem

처음 보는 AI workload는 장치가 등록됐다는 사실만으로 실행 가능성이나 적합한
CPU·메모리·runtime 구성을 알 수 없다. 모델 이름, 이미지 이름 또는 GPU
utilization만으로 이력을 재사용하면 품질·환경·입력 차이를 놓칠 수 있다.
실행 가능한 후보를 검증하고 제한된 측정 예산 안에서 근거를 얻는 문제를
자원 결정의 중심에 둔다.

연구 질문은 제한된 profiling의 유효 구성 발견(RQ1), 의미와 품질을 유지한
자원 선택(RQ2), 초기 비용을 포함한 반복 실행 순효과(RQ3), drift/OOD 시
재확인(RQ4), 서로 다른 native scheduler 위의 공통 계약(RQ5)이다. 유리한
결과가 나오는 것이 acceptance 조건은 아니다.

## 2. Why existing schedulers do not solve this question

Kubernetes는 container 실행과 node binding, Kueue는 admission·quota·priority,
Slurm은 partition/QOS/account 정책과 batch scheduling을 담당한다. 이 기능은
이 프로젝트의 개발 기여로 표현하지 않는다. MLflow, Prometheus, device
plugin과 HAMi도 기존 기반 기술이다.

Resource Advisor는 workload/runtime compatibility, 다음 profiling 후보,
측정 근거와 불확실성, 추천의 유효 기간, 승인 실행의 actual/reference 차이를
연결한다. Native scheduler의 최종 placement/admission 권한을 유지한다.

## 3. Method

Compatibility → observe/cooperative pilot → comparable profile → budgeted
active search → independent confirmation → recommendation → explicit approval
→ native execution → result/usage/profile → feedback → reuse/invalidation.

이력이 없는 workload는 `NEEDS_PROFILE`로 abstain한다. Observe는 원본 코드의
외부 관측이며 모르는 step time·throughput·quality는 unknown이다. Pilot은
연구자가 제공한 bounded entrypoint, 명시적 consent, 후보·시간·장치 예산과
출력/checkpoint 보호가 있어야 실행한다. 전체 후보를 실행하거나 사용자 모델을
임의 변환하는 것이 기본 경로가 아니다.

Right-sizing은 의미·품질 조건을 유지하며 승인된 변수를 목적 함수에 맞게
탐색하는 것이다. 이번 GPU trial은 accelerator count=1, precision/model/data/
work units를 고정하고 host CPU와 host memory만 변경한다. DDP/global batch/
checkpoint/scaling qualification 없는 GPU count 탐색은 포함하지 않는다.

## 4. Methodologies

- Lookup, seeded random, 실제 BoTorch constrained qLogNEI를 재사용한다.
  측정 observation만 학습하며 실패/OOM을 runtime=0으로 만들지 않는다.
  범주를 ordinal 성능으로 인코딩하지 않고 모델/계획 시간을 비용에 남긴다.
  이번 BB-source trial은 fitting/acquisition/import를 합친 `planning_seconds`만
  측정했다. 이후 source는 fitting과 acquisition subphase도 별도 기록한다.
  과거 측정에서 subphase를 분리한 수치를 추정해 넣지 않는다.
- Adaptive replication은 min/max repeats, stopping rule, noise와 독립 확인
  예산을 갖는다. 기존 실험은 equal-budget 우위를 보여 주지 않았다.
- MF-KG는 preregistered paired fidelity calibration이 통과한 그룹에만 허용한다.
  현재 실제 GPU 그룹은 qualification을 통과하지 못했다. Numerical kernel과
  scheduler double 검사는 실장비 MF-KG 최적화 성과가 아니다.
- Transfer/RGPE는 승인 family, provenance, target check와 source validity가
  필요하다. 기존 실제 비교는 네 전략이 같은 CPU 구성을 선택했다.
- Scope, repeat 수, age, quality/memory regression과 drift를 확인한다. 평균의
  descriptive interval은 calibrated future prediction interval이 아니다.
  환경이 회복됐다는 이유로 무효화된 추천을 자동 부활시키지 않는다.

## 5. Implementation

기존 [contracts](../src/resource_advisor/contracts.py),
[compatibility](../src/resource_advisor/policy.py),
[durable study](../src/resource_advisor/study.py),
[BoTorch search](../src/resource_advisor/search.py),
[service](../src/resource_advisor/service.py),
[native adapters](../src/resource_advisor/backends.py),
[uncertainty](../src/resource_advisor/uncertainty.py),
[accounting](../src/resource_advisor/accounting.py)을 재사용한다.

새 [right_sizing](../src/resource_advisor/right_sizing.py) 모듈은 project-scoped
read-only lifecycle API와 terminal transaction 안의 immutable feedback을 추가한다.
추천·source profile·workload/context·native ID·result digest·usage ID·residual을
같이 보존한다. 실패/취소/invalid result는 censored 결과이며 비용은 ledger에
남긴다. 기존 SQL entity table을 사용하고 과거 receipt를 만들어 넣지 않는다.
[API와 rollback 계약](right-sizing-api.md)에 범위를 명시했다.

Optional model/optimizer/input range/host CPU/runtime flag 서술자는 새 scope에
포함되며 absent field는 legacy serialization에서 생략한다. 따라서 기존
signature가 유지되고 선언한 input range가 미검증 shape를 허용하지 않는다.

## 6. Hardware and workloads

기존 실장비 증거는 RTX5080 CUDA/PyTorch, Slurm Orin GPU, Hailo8 ResNet50과
KFP→API→Kueue→GPU→Result다. 새 trial은 접근 가능한 RTX5080과 Hailo8을
사용한다. [갱신된 hardware matrix](right-sizing-hardware-matrix.md)와 [고정 계획](right-sizing-trial-plan.md)은 W1 synchronized matmul,
W2 input/preprocess 포함 generated CNN, W3 qualified Hailo ResNet50을 정의한다.

W1/W2는 CPU 0.5/1/2 × memory 1/2GiB의 여섯 구성이다. W3는 기존 단일
qualified 구성의 원본 observe/approval 경로다. **W3와 W1/W2는
NOT_COMPARABLE**이며 GPU/NPU 속도 순위를 합치지 않는다. AMD는 실제 장비
증거가 없고 DEEPX는 탐지/runtime/model 실행 미확인 상태로 supported가 아니다.

## 7. Baselines

Static/User-selected는 같은 native queue의 합리적인 CPU1/memory2GiB 구성이다.
Random과 qLogNEI는 각 최대 5 probes, 같은 wall/device budget, 보호된 독립
confirmation 예산을 사용한다. 반복 main 실행은 세 temporal blocks다.
전체 finite grid는 search/main 결정 뒤 별도 characterization으로 측정하며
사전에 알고 있던 oracle이나 equal-budget 경쟁자로 표현하지 않는다.
조건을 통과하지 못한 MF-KG/RGPE 확장은 비교군으로 억지 실행하지 않는다.

## 8. Results

새 [GPU 원시 캡처](evidence/right-sizing-gpu-v1.json)와
[재계산 auditor](evidence/right-sizing-gpu-audit-v2.json)는 F0 4개와 API Job 97개를
포함한다. 신규 history 없는 W1/W2가 먼저 abstain하고, 실제 pilot→profile→
추천→명시적 승인→native 실행→feedback을 통과했다. 4 studies에서 Random과
qLogNEI는 각각 5 probes를 사용했다. BoTorch acquisition이 실제 다음 Job을
선택한 경우는 W1/W2 각 2회, 총 4회였고 fallback은 없었다.

| Workload | Static mean (s) | Random mean (s) | BO mean (s) | 두 탐색의 선택 |
|---|---:|---:|---:|---|
| W1, 100 synchronized matmul 합계 | 0.046739913 | 0.046748409 | 0.046611692 | CPU1 / memory2GiB |
| W2, 12 CNN blocks 합계 | 0.263764764 | 0.260848683 | 0.260731440 | CPU2 / memory2GiB |

각 main arm은 새 native Job 3개다. BO의 static 대비 평균 차이는 W1 -0.2743%,
W2 -1.1500%지만, **Random과 BO가 같은 구성을 선택했으므로 BO의 성능 우위나
통계적 유의성을 주장하지 않는다**. 기존 reference v1은 독립 확인에서 native
`DeadlineExceeded`가 발생해 두 study 모두 abstain했다. 컨테이너가 exit 0이어도
실패 Job을 유효 성능으로 채택하지 않았다. 원시 실패 2개와 비용을 유지한다.

[추가 reference v2 계획](right-sizing-reference-recovery-plan.md)은 별도 ID로
실행 전에 고정했다. 최초 search/main 선택·후보·품질 조건을 바꾸지 않고,
reference confirmation 보호 예산만 늘렸다. 양쪽 workload에 같은 새 reference를
사용하며 v1의 실패를 덮어쓰지 않는다. [별도 reference 캡처](evidence/right-sizing-reference-recovery-v2.json)와
[cross-report audit](evidence/right-sizing-comparison-v1.json)는 W1 전체 여섯 구성의
3회 독립 확인을 검증했다. W1에서 두 전략의 선택은 뒤에 측정된 최소 평균보다
0.6428% 길었다. 이는 같은 환경에서 나중에 얻은 finite reference의 서술적
거리이며 실제 oracle, 우위나 near-optimal 확률 보장이 아니다. W2 v2는 첫 3개
confirmation 중 native deadline 실패가 있어 abstain했다. W2의 regret은 null이다.
이후 source에 같은 후보의 실제 pilot 시작 시간을 보는 보수적 preflight를 추가했다.
측정된 startup이 cap을 이미 다 쓰면 `INSUFFICIENT_NATIVE_STARTUP_BUDGET`으로
추가 제출을 보류하며, native limit/승인 시간 예산을 임의로 늘리지 않는다.
이 guard는 software 검증이며 이번 고정 BB-source hardware 결과를 바꾸지 않는다.

GPU 승인 실행 12개에 immutable comparable feedback이 생성됐다. 실행 이후
다음 read-only lookup은 W1의 실제 main profile 6개, W2의 9개를 근거로 사용했다.
추천 4개는 실제 TTL 만료 뒤 재사용 요청에서 모두 422로 거부됐고 새 Job은
0개였다. [expiry 캡처](evidence/right-sizing-stale-reuse-v1.json)를 보존한다.

새 Hailo [원시 실행](evidence/right-sizing-hailo-v1.json)은 qualification 1회와
API 실행 4회다. 독립 승인 실행의 actual/reference residual이 DB에 남았고
S3/API/MLflow bytes를 다시 읽어 일치 여부를 확인했다. 평균 call time
0.339591987초에 대해 actual 0.340695216초, 차이 +0.324869%였다.
[구체적인 범위와 결과](right-sizing-hailo.md)를 참고한다.

## 9. Profiling Cost and Net Benefit

`TotalCost(N) = QualificationCost + ProfilingCost + ConfirmationCost +
Σ MainExecutionCost(1..N)`으로 평가한다. Study wall cost에는 model fitting,
confirmation과 control waits가 이미 포함되어 있으므로 다시 더하지 않는다.
GPU/NPU 예약초·CPU core초·wall time은 별도 축이며 서로 같은 비용 단위가 아니다.
실제로 측정한 N까지만 cumulative curve를 만들고 손익분기를 외삽하지 않는다.

실제 N=3의 cumulative 비용은 다음과 같다. `Latency cost`는 F0 각각의 Job
latency + study elapsed(모델/확인/control 포함) + main API lifecycle latency다.
동시에 제출한 F0의 대기 시간이 겹칠 수 있으므로 전체 실험의 단일 wall-clock과
같지 않다. [원시 비용 정의](evidence/right-sizing-gpu-v1.json)를 그대로 사용한다.

| Workload / arm | N=3 latency cost (s) | GPU reservation (s) | CPU reservation (core-s) |
|---|---:|---:|---:|
| W1 static | 56.463414 | 13 | 15 |
| W1 random | 274.443631 | 34 | 37 |
| W1 BO | 274.687254 | 36 | 40.5 |
| W2 static | 77.892482 | 13 | 16 |
| W2 random | 376.872685 | 45 | 64.5 |
| W2 BO | 375.268462 | 43 | 63 |

**Random과 BO 모두 실제 N=1..3에서 profiling 비용을 회수하지 못했다.**
측정하지 않은 N까지 손익분기를 외삽하지 않는다.
[누적 비용 그림](evidence/right-sizing-figures-v2/right-sizing-cost-v2.png),
[reference 그림](evidence/right-sizing-figures-v2/right-sizing-reference-v2.png),
[CSV](evidence/right-sizing-figures-v2/right-sizing-cost-v2.csv)와
[figure provenance](evidence/right-sizing-figures-v2/right-sizing-figures-v2.json)는
원시 JSON에서 auditor를 거쳐 생성한다. 곡선은 N=1..3만, reference는 모든 유효
fresh confirmation 점을 보여 준다. W2 incomplete label은 결과가 없는 후보를
0초로 표시하지 않는다는 뜻이다. Primary GPU 연구 전체는
101 native Jobs, 285 GPU 예약초로, 실패한 v1 reference의 비용도 포함한다.
Reference v2는 추가 45 Jobs, 136 GPU 예약초다. 합계 GPU 146 Jobs / 421 GPU
예약초이며 native deadline 실패 3개를 포함한다. Hailo까지 이번 새 연구 실행은
151 native Jobs다. Reference 비용은 별도 연구 사용량과 총계에 모두 보고하며,
first-use 배포 곡선을 유리하게 바꾸지 않는다. Hailo protocol은 73.276937초, 5 native Jobs, 14 NPU 예약초와
14 CPU core초다. GPU와 NPU 예약초를 경제적으로 동등한 숫자로 합치지 않는다.
[총 연구 비용 auditor](../examples/audit_right_sizing_cost.py)와
[재계산 기록](evidence/right-sizing-total-cost-audit-v1.json)은 CPU 500.5 core초,
GPU 연구 Job의 host-memory 721,920 MiB초, 원래 controller 시작부터 마지막
reference terminal 관측까지 4,195.255873초를 보고한다. Hailo는 그 시간 안에
병행 실행돼 73.276937초를 전체 wall에 다시 더하지 않는다. 실험 종료 뒤의
artifact readback·배포·소프트웨어 테스트는 이 envelope 밖이다.
기존 fixture/image construction과 에너지는 이번에 측정하지 않았으므로 0으로
쓰지 않는다. [Hailo 비용](evidence/right-sizing-hailo-cost-v1.json)에 이를 명시했다.
기존 [operational comparison](operational-comparison.md)의 compute 약 1% 단축,
실제 6회 반복에서 profiling 비용 회수 실패도 유지한다.

## 10. Negative and Null Results

기존 qLogNEI는 random/lookup 대비 우위가 확인되지 않았다. Transfer의 네
전략도 같은 configuration을 선택했다. Fidelity calibration은 실제 GPU 그룹을
qualify하지 못했다. Recommendation mean interval의 미래 실행 coverage도
보장되지 않았다. 새 Hailo 승인 실행 역시 좁은 평균 구간 밖이었다.
실패한 predecessor와 invalid Slurm verification의 비용을 삭제하지 않는다.

[W2 phase 원시 증거](evidence/right-sizing-phase-v1.json)와
[auditor](evidence/right-sizing-phase-audit-v1.json)는 CPU 준비 비중이 static 4.57%,
BO 3.14%임을 보여 준다. Accelerator 경로 비중은 약 95~97%지만 CUDA launch,
device 실행과 synchronization이 함께 포함되므로 순수 kernel busy time이나
확정적인 원인 진단이 아니다. NVML 읽기 시간/측정 objective 비율은 약 1%지만
collector-on/off 실험이 없어서 전체 instrumentation slowdown으로 해석하지 않는다.
W1 phase trace, 새 job-attributed eBPF/PSI와 장기 thermal qualification은 unknown이다.

새 Hailo의 중간 lifecycle projection은 옛 cold-start 기록 때문에 새로운
profile을 가렸다. 코드와 regression test를 수정했지만 고정된 실험 중간에는
실배포를 교체하지 않았다. Completed Pod cleanup에 따른 외부 로그 수집
중단은 저장된 native termination receipt와 같은 Job ID로 복구한다. Replay로
유리한 결과를 선택하지 않는다.

## 11. Contributions

현재 증거가 있는 기여는 qualified runtime compatibility, budgeted profiling과
independent confirmation의 결합, explicit approval과 native execution 연결,
새 immutable actual/reference feedback, cost-aware 평가 및 null result 보존이다.
새 GPU 12개와 Hailo 1개의 approved feedback, source-history 재사용과 expiry 거부가 실제로 검증됐다. 최종 claim
gate는 [claim audit](right-sizing-claim-audit.md)에서 계속 갱신한다.
BO 우위와 profiling 순이익은 검증되지 않으면 방법 구현 및 null result로 보고한다.

## 12. Limitations

현재 Slurm controller는 network unreachable이고 worker의 `sinfo` RPC가
timeout이다. 기존 Slurm native 실행과 response-loss/crash recovery 증거는
보존하지만 새 feedback loop의 Slurm 실장비 완료는 **BLOCKED**다. 재개에는
controller 연결, authenticated native inventory/accounting, qualified runtime과
같은 승인 실행의 result/usage/feedback 검증이 필요하다. Running scheduler,
driver, KubeEdge 설정을 바꿔 이 문제를 숨기지 않는다.

동일 logical GPU/NPU 성능 비교, AMD 실측, DEEPX qualification, 실제 DDP
right-sizing, large-cluster 일반화, shared-GPU interference predictor와 job-attributed
eBPF diagnosis 성능은 주장하지 않는다. 고정 생성 workload의 측정 결과를 임의
사용자 AI 코드의 최적 성능 예측으로 일반화하지 않는다.


재현 명령(새 output 경로 사용):

```sh
uv run python examples/audit_right_sizing_trial.py docs/evidence/right-sizing-gpu-v1.json docs/evidence/right-sizing-trial-plan-v1.json
uv run python examples/audit_right_sizing_trial.py docs/evidence/right-sizing-gpu-v1.json docs/evidence/right-sizing-trial-plan-v1.json --reference docs/evidence/right-sizing-reference-recovery-v2.json --reference-plan docs/evidence/right-sizing-reference-recovery-plan-v2.json
uv run python examples/audit_right_sizing_cost.py docs/evidence/right-sizing-total-cost-capture-v1.json
uv run --with matplotlib==3.11.1 python examples/plot_right_sizing.py docs/evidence/right-sizing-gpu-v1.json docs/evidence/right-sizing-trial-plan-v1.json /tmp/new-right-sizing-figures --reference docs/evidence/right-sizing-reference-recovery-v2.json --reference-plan docs/evidence/right-sizing-reference-recovery-plan-v2.json --version v2
```

근거 있게 채택할 문장은 다음 범위다.

> 처음 등록된 GPU·NPU AI 워크로드의 실행 조건을 검증하고, 승인된 profiling과
> 실측 기반 탐색·독립 확인을 Kubernetes/Kueue 실행에 연결해, 추천 근거·실행
> 오차·비용·유효성을 다음 자원 판단에 반영하는 profile-guided resource advisor
> 계층을 구현·검증했다.

각 구절은 [claim audit](right-sizing-claim-audit.md)의 cold-start, compatibility,
actual acquisition, confirmation, approved feedback, post-main lookup, expiry
행에 연결한다. Slurm 공통 계약과 기존 실행 복구는 재사용했지만 이번 새
Slurm feedback hardware gate가 막혀 있어 목표 문장 전체를 채택하지 않는다.
