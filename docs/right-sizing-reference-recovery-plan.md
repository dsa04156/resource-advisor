# 별도 finite-reference 복구 프로토콜

새 experiment ID: `right-sizing-reference-recovery-20261007-v2`.
[실행 전 고정 계획](evidence/right-sizing-reference-recovery-plan-v2.json).

원래 trial의 W1 reference는 native `DeadlineExceeded` 때문에 abstain했다.
해당 컨테이너는 exit 0이었지만 Job은 실패였으므로 승인 가능한 성능 결과로
처리하지 않는다. Native active deadline은 startup까지 포함하는 7초였고,
confirmation/control overhead가 확인용 시간 여유를 줄였다. 실패와 비용은
원래 experiment ID에 그대로 남긴다.

추가 프로토콜은 실제 finite reference를 완성하기 위한 **별도 characterization**이다.
Random/BO 탐색 예산, 그때 선택한 구성, model/data/precision/quality와 여섯
CPU·memory 후보를 변경하지 않는다. 각 후보 2 probes + 3 fresh confirmation,
각 workload 총 900초 / 900 GPU 예약초 cap 안에서 confirmation reserve만
500초로 사전 고정한다. 두 workload 최대 60개의 새로운 native Jobs다.

두 workload 모두 이 새 v2 reference를 사용하도록 결과 전에 정한다.
유리한 v1/v2 결과를 골라 쓰지 않는다. V2가 불완전하면 finite-space regret은
unknown이다. 기존 fresh qualification은 재사용하되 observed timestamp와 TTL을
늘리지 않는다. 추가 characterization 비용은 전체 protocol 사용량에 별도로
포함하고, 제한된 advisor의 first-use 이익으로 숨기지 않는다.

이는 equal-budget Random/BO 비교의 예산을 늘리는 조치가 아니다. 새 reference를
이전 optimizer의 training history로 넣지 않고, 원래 추천·승인·실행 기록도
다시 작성하지 않는다. 선택에 우위가 없거나 profiling 비용을 회수하지 못한
결과는 그대로 보고한다.
