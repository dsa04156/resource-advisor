# CNN finite-reference 보완 실험 v3

실행 전 고정한 계획은 [JSON](evidence/right-sizing-reference-plan-v3.json)이다.
원래 Random/qLogNEI 선택, 예산, main 결과와 실패한 v1/v2 reference는 변경하지 않는다.

- 질문: 같은 CNN 의미·품질·6개 CPU/memory 후보에서 독립 확인을 완료할 수 있는가?
- 변경: native Job deadline에 포함되는 startup을 고려해 confirmation reserve를
  1,500초, study wall/device budget을 2,100초로 사전 고정한다.
- 새 qualification 2회 + pilot 12회 + confirmation 18회, 최대 32개 native Jobs.
- 기존 capability는 만료됐으므로 실제 runtime/model 실행으로 새 capability를 만든다.
  runtime/context/digest가 달라지면 비교를 중단한다.
- 한 번의 declared study만 실행한다. 성공할 때까지 반복하거나 실패 측정을 되살리지 않는다.
- benchmark source는 기존 immutable ConfigMap의 bb2f834, controller는 startup guard가
  포함된 1b1bb48이다. controller 변경을 성능 향상으로 해석하지 않는다.
- 독립 단위는 native Job이다. 3회 confirmation을 각 후보에 배정하고 native planner의
  seed 20261008 순서를 기록한다. 별도 balanced layout은 설계 자료이며 planner와
  동일한 순서라고 주장하지 않는다.
- v3는 다음 날의 descriptive finite reference다. 동시 측정 oracle, optimizer 우위,
  calibrated prediction을 증명하지 않는다. W1의 완성된 v2는 별도로 유지한다.
- qualification, 실패·invalid 결과, confirmation의 예약 비용까지 새 ledger와 raw evidence에
  포함한다. 기존 profiling 비용 회수 실패 결론을 새 reference로 덮어쓰지 않는다.

완료 상태와 수치는 실제 실행 뒤 별도 artifact에 추가한다.
