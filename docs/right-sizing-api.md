# Right-sizing lifecycle and feedback contracts

`GET /api/v1/compute/workloads/{ref}/right-sizing` is project-scoped and read-only.
It joins existing immutable workload, compatibility, profile, study,
recommendation, approval, execution and feedback records. It creates no
recommendation or execution. The recommendation preview uses the existing
ranker without persisting its temporary result; it grants no approval authority.

`UNKNOWN`, `COMPATIBILITY_CHECKED`, `NEEDS_PROFILE`, `PROFILING`, `PROFILED`,
`RECOMMENDABLE`, `RECOMMENDED`, `APPROVED`, `EXECUTED`, `VERIFIED` are evidence
projection milestones, **not new native scheduler or Job states**. A milestone
does not manufacture a historical transition timestamp. The response gives
current state/reasons, source IDs, exact signatures, candidates, descriptive
ranking intervals, scope status, current recommendation validity and feedback.
`VERIFIED` means a comparable independent actual/reference comparison exists;
it does not mean a faster configuration or calibrated prediction. Expiry,
qualification failure or drift can supersede that historical verification.

Unseen qualified workloads return `NEEDS_PROFILE`, no recommendation reference
and `additional_profiling_required=true`. Unsupported variants remain excluded.
Scope status is exact workload/environment evidence scope, not a learned OOD
probability. Predictive interval calibration remains `NOT_QUALIFIED`.

For every newly terminal **approved** execution, the same terminal/usage
transaction stores one `ra_entities` record of kind `recommendation_feedback`,
keyed by attempt ID. It freezes approval/recommendation digests, source profile
IDs, signatures, native ID, result digest, usage ID, terminal state, quality and
memory status, measured reference mean/interval, actual result and signed
residual. Missing/invalid/failed/canceled measurements stay censored, with null
residual and explicit reasons. Their terminal usage record is still created.
Duplicate result ingestion returns the original receipt and ledger; no rewrite.

No SQL table migration is needed: this adds a bounded record kind to the existing
immutable entity table. Older records are not backfilled or assigned fictional
historical receipts. Existing read-time residual/drift checks continue to work
for old Jobs. Rolling back the application leaves new receipts intact; old
code ignores the additional entity kind. New receipts are created only by a
worker/API process running the new terminal transaction code, so deployment of
an API alone does not prove worker-generated hardware feedback.

Workload identity optionally records `model_name`, `optimizer`,
`optimizer_parameters` and `input_shape_range`. Execution context optionally
records `host_cpu_model` and `runtime_flags`. Absent fields are omitted from
serialization to retain every legacy signature. New nonempty descriptors change
the signed scope and cannot reuse legacy history automatically. Declaring an
input range does **not** qualify any new shape: exact RuntimeVariant shape and
logical-workload checks still apply. Missing host/optimizer information is not
inferred. These fields document approved semantics, not permission to alter them.

```sh
uv run pytest -q tests/test_right_sizing.py tests/test_right_sizing_audit.py
uv run python examples/audit_right_sizing.py --output /tmp/new-baseline-audit.json
```

The composite auditor reuses six domain auditors and additionally validates raw
policy result envelopes, independent IDs and native chronology. Its input hash
manifest identifies exactly which preserved files were audited. It does not
contact private hardware/storage and explicitly reports zero new hardware Jobs
and an open overall goal. Mutation tests reject corrupt digests, missing costs,
duplicate attempts, changed budgets and reversed/future native timestamps.


## Native startup budget

Kubernetes `activeDeadlineSeconds` starts when a suspended Job resumes, before
container execution. It remains equal to the approved `max_run_seconds`; queue
allowance is not silently added to that cap. New confirmation plans inspect
only same-candidate, same-study completed hardware pilot timestamps. If observed
server-submission-to-container-start time already exhausts the next native cap,
the study abstains with `INSUFFICIENT_NATIVE_STARTUP_BUDGET` and source attempt
IDs, before submitting another Job. This interval conservatively includes
admission and is not a calibrated startup forecast. Missing timestamps are
unknown; an unseen startup delay can still fail. The fixed v1/v2 hardware trials
used the previous committed source, so this guard has software verification,
not a claim of retrospectively preventing their recorded failures.
