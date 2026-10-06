# Two-project Slurm policy: partial live acceptance

The [protocol](slurm-project-isolation-plan.md) was committed before adding a
second identity or submitting its GPU Jobs. [Public evidence](evidence/slurm-project-isolation-v1.json)
retains the incomplete outcome. Do not restart this cohort with different keys.

## Implemented and observed

The lab now has two distinct Linux owners, Slurm accounts/partition associations,
forced-command SSH keys, mode-0700 result directories and trusted worker routes.
Both use the same previously qualified Orin GPU and immutable native sm_87 CNN
runtime. Each account has an aggregate ceiling of one CPU/1GiB/one GPU; this is
not a reserved GPU entitlement. Existing QOS, project A gateway, native config
hashes and daemon identities were preserved during provisioning. The
[Ansible role](../automation/ansible/SLURM_PROJECT.md) repeated with zero changes;
its unauthorized-lab negative check failed before mutation.

Both owners' two-CPU, 2GiB, unassigned-QOS and foreign-account requests were
rejected by the actual native scheduler: eight rejections, no accepted jobs.
This was not an SSH failure or mocked quota check. Common SchedulingProfiles
mapped to the actual account/partition/normal or high QOS without a researcher
selecting a GPU. B's capability/profile refs are independent because the
registry's references are globally unique; a setup collision was retained and
corrected without changing A's capability or submitting compute.

Both directions observed an occupied GPU, older normal pending, later high
pending, then high running while the older normal remained pending. Round one
completed all three results. Round two completed its holder and high result;
the last normal Job remains unresolved after a transport interruption.

Foreign API Job reads/cancels/artifacts returned 404. Actual forced gateways
hid the other owner's queue/accounting/result data. Independent native queries
confirmed both active jobs were unchanged after foreign cancellation commands;
`scancel` exit zero alone was not treated as isolation proof. Project Job/usage
lists were also checked. These are bounded ownership checks, not hostile-tenant
isolation or direct MLflow/S3 tenant authorization.

## Results, costs and interruption

| Original attempt | Observed state | Verified GPU reservation seconds | Queue seconds |
|---|---|---:|---:|
| B qualification | SUCCEEDED | 72 | 1 |
| Round 1 A holder | SUCCEEDED | 73 | 0 |
| Round 1 B normal | SUCCEEDED | 73 | 143 |
| Round 1 A high | SUCCEEDED | 75 | 61 |
| Round 2 B holder | SUCCEEDED | 72 | 1 |
| Round 2 B high | SUCCEEDED | 73 | 62 |
| Round 2 A normal | CANCEL_REQUESTED, termination unknown | unknown | unknown |

Six completed attempts each have one validated result, one project ledger and
one original FINISHED MLflow run, with identical S3/API/MLflow bundle bytes and
foreign artifact denial. All pass the fixed numerical comparisons; they do not
measure trained-model accuracy. The five observed completed application
allocation intervals do not overlap. Full six-application accounting remains
open. The known completed cost is **438 GPU reservation seconds**, including
qualification; final attempt cost is unknown, not zero. Runtime package
verification dominates these reservations; millisecond kernel time is not the
whole resource cost. Utilization, power and temperature remain unmeasured.

The independent observer lost SSH connectivity while the final normal Job was
RUNNING. The original verifier requested cancellation and stopped with
INCOMPLETE. A later readback found SSH `No route to host`, both deployed worker
routes unable to query native queues, and the API still CANCEL_REQUESTED with
no result/ledger and one undelivered outbox entry. The worker Pod itself was
Ready with zero restarts. A cancellation request does not prove native
termination. No new compute, reboot, driver or scheduler change was attempted.

A subsequent read-only query reached the original GPU worker directly. Its
Slurmd remained active with the same recorded process identity, while controller
SSH and Slurmctld ports were unreachable from that worker. The final attempt's
original-owner result file exists: one COMPLETED hardware envelope with quality
1.0, matching job/attempt/epoch/workload/context and result digest. This proves
the cooperative computation produced a valid output, not that the parent
allocation ended or its accounting was delivered. The API still has no persisted
result/ledger. The output was saved privately; no manual state change or result
ingestion was used to bypass scheduler confirmation. Reconcile this same attempt
when the controller returns, preserving completion if it won the cancel race.

The verifier now retries observation on the same saved IDs within its bounded
deadline, skips priority predicates on failed samples, and preserves jobs when
native observation remains unavailable. Two targeted regression tests passed:
disconnection creates no extra attempt/cancel, and recovery uses the same IDs.
This fix does not undo the original cancellation or turn this trial into PASS.

Remaining acceptance is to reconnect, inspect the same final Job, confirm its
terminal native state/cost and delivery, then verify native idle and unchanged
source state. If it was canceled, retain that outcome rather than claiming six
successful results. Overall E6, the full failure matrix, physical Pi NPU and
complete platform acceptance remain open.

Read-only recovery uses the original private report and a new output path:

```sh
uv run python examples/verify_slurm_project_isolation.py \
  --config /secure/original-priority-config.json \
  --original-report /secure/original-priority-trial.json \
  --report /secure/new-priority-readback.json
```

This mode refuses missing/duplicated original identities, checks all six scoped
API IDs and queries only their original native IDs. It never submits or cancels,
retains the original report byte-for-byte and reports INCOMPLETE on observer loss.
An actual current-controller readback remains INCOMPLETE. Four targeted transport/
recovery tests pass. Even NATIVE_RECONCILED would still require result/ledger
publication, native idle and source preservation; command exit zero is not full
acceptance. If the last native outcome is canceled, do not restart or relabel it.
