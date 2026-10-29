# Integrated operations (Kubernetes + Slurm)

Direction (2026-10-29): reliable operation of existing native schedulers before
any new scheduling algorithm. Order: lifecycle → usage → native policy → fault
recovery. This directory documents what the software now does and what remains
to be shown on owned lab hardware. It does not replace `docs/PROJECT-PURPOSE.md`.

| Document | Content |
|---|---|
| [CURRENT-STATE-AUDIT.md](CURRENT-STATE-AUDIT.md) | What already existed, verified gaps G1–G8, chosen minimum changes |
| [ARCHITECTURE.md](ARCHITECTURE.md), [architecture.svg](architecture.svg) / [.png](architecture.png) | Responsibility boundaries, lifecycle/accounting/policy contracts |
| [PLAN.md](PLAN.md) | Frozen fault/usage/policy plan and budget |
| [RUNBOOK.md](RUNBOOK.md) | Build, rollout, trial, rollback |
| [RESULTS.md](RESULTS.md) | Software-phase results; live results section empty until run |

## What changed (code + tests; deployed to the owned lab 2026-10-29, see RESULTS)

- Every reconcile of an uncertain submission is recorded (`MATCHED`, `NO_MATCH`,
  `AMBIGUOUS`, `QUERY_FAILED`); only successful no-match queries accumulate.
- `GET /jobs/{id}/lifecycle`: native identity, reconciliation history, native
  wait category, admission, usage presence, acknowledgement eligibility.
- `POST /jobs/{id}/submission-resolution` (`ACKNOWLEDGE_UNRESOLVED`): operator
  with subject, same project, after ≥2 consecutive no-match. Immutable audit;
  the attempt stays open, is never resubmitted, and a late native job is adopted.
- Definitive scheduler refusals of the submit command (exact Slurm lines, e.g.
  `Job violates accounting/QOS policy`, `Invalid qos specification`; Kubernetes
  `Forbidden`/`Invalid`) become terminal `FAILED` with `NATIVE_REJECTED:<code>`
  and proven zero allocation. Timeouts, SSH failures and `AlreadyExists` stay
  uncertain. Previously all of these were `SUBMISSION_UNKNOWN` forever.
- Multiple/foreign native matches are `AMBIGUOUS`, not transport errors.
- `GET /usage/report?month=YYYY-MM` or `start/end` with explicit offsets: UTC
  `[start, end)` clipping, unknown counted not zeroed, not-finalized attempts
  listed separately, groups split by cluster/device class/allocation mode/model/
  resource key. Optional institution scope from trusted operator credentials.

## NAIS narrative (≈55 s)

"우리는 새 스케줄러를 만들지 않습니다. Kueue와 Slurm이 승인·쿼터·우선순위를
결정하고, Resource Advisor는 그 위에서 작업 하나하나의 신원과 의도를 먼저
데이터베이스에 남깁니다. 제출 응답이 사라져도 같은 시도 이름으로 스케줄러를
다시 조회해 같은 작업을 이어받고, 다시 제출하지 않습니다. 조회가 실패하거나
결과가 애매하면 '모른다'고 기록하고, 운영자는 근거를 보고 감사 기록이 남는
확인만 할 수 있습니다. 정책 위반으로 거부된 제출은 대기와 구분해 0 할당으로
종료합니다. 종료 상태와 사용량 한 건은 같은 트랜잭션으로 기록되어 재시작이나
재조회로 이중 과금되지 않습니다. 월별 보고서는 UTC 경계에서 예약 구간을
잘라 계산하고, 모르는 값은 0이 아니라 '미확인'으로 보여줍니다. GPU, 공유 슬롯,
NPU는 서로 합치지 않습니다. 실제 장애 복구 시연은 소유한 CPU 경로에서
제한된 작업 수로 진행하며, GPU 성능 주장은 하지 않습니다."

## Limitations

- Deployed and read back on 2026-10-29; no new native workload ran (bounded trials
  NOT_RUN, see RESULTS). CPU/VM trials cannot support GPU-hour
  or GPU performance claims. Physical Slurm is unreachable.
- Acknowledgement does not prove absence. A native job submitted but invisible to
  queries remains possible; its usage appears only if it is later matched.
- Kubernetes Jobs are bound by attempt annotations and UID; a recreated same-name
  Job is an observation error, never adopted. Slurm reconcile reads accounting
  `--duplicates` with Submit; distinct (ID, Submit) records are AMBIGUOUS.
- Slurm reconcile identifies the attempt by its unique attempt name, account and
  partition (the route's project mapping); the `--comment` owner tag is not
  queried because forced-command gateways pin output formats.
- Pod termination retention remains opt-in (not enabled on live-training routes);
  deleted Pods there still yield unknown duration.
- Kueue never-fitting requests stay `QUOTA_OR_LIMIT_PENDING` (native decision
  preserved); they are not reclassified as rejections.
- One worker replica per route is the qualified configuration; no HA claim.
