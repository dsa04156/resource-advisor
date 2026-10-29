# Results

## Software phase (2026-10-29) — code + automated tests only

Level: code exists and related tests pass with explicit scheduler doubles and
SQLite. Not deployed; no native job was created; no hardware result.

Commands (worktree root, Python 3.13, `uv sync --locked --extra pipelines --extra optimizer --extra artifacts`):

```sh
uv run pytest -q tests/test_integrated_operations.py              # 47 passed
uv run pytest -q tests/test_integrated_operations.py tests/test_worker.py \
  tests/test_accounting.py tests/test_backends.py tests/test_slurm_executor.py \
  tests/test_concurrent_worker.py tests/test_api_flow.py tests/test_configuration.py \
  tests/test_slurm_ownership.py tests/test_kubernetes_retention.py \
  tests/test_database_recovery.py tests/test_owner_lease.py tests/test_service.py \
  tests/test_console.py tests/test_operations.py                  # focused regression, all passed
uv run pytest -q                                                   # 1463 passed, 11 skipped
uv run ruff check src tests examples/integrated_operations_trial.py examples/slurm_executor.py
```

The 11 skips are PostgreSQL-only tests (`RA_TEST_DATABASE_URL` not set). The
full count is the base suite plus the 47 new tests; the original
`resource-advisor` worktree has additional untracked VLM tests, so its count
differs without indicating a regression. Remaining `ruff check .` findings are in
pre-existing `docs/nais-interview` and `docs/presentation` scripts, unchanged here.

| Required case | Test |
|---|---|
| Lost submit response, native created, DB missing ID | `test_lost_response_with_native_job_adopts_once_and_records_match` |
| Pre-call crash (intent committed, no native call) | `test_pre_call_crash_is_uncertain_and_never_resubmitted` |
| Stale claim / leased event | `test_resolution_does_not_steal_a_leased_event`; existing `test_postgres_claim_and_stale_finish_are_fenced` |
| Restart + terminal result, repeated reconcile, no double usage | `test_restarted_worker_and_repeated_reconcile_add_no_usage` |
| Cancellation race on uncertain submission | `test_cancel_of_uncertain_submission_never_claims_confirmation`; existing completion-wins tests |
| Unknown native query / ambiguity | `test_failed_or_ambiguous_query_resets_evidence`, `test_slurm_reconcile_multiple_matches_is_ambiguity` |
| Acknowledgement evidence, audit, late adoption | `test_acknowledgement_needs_evidence_and_keeps_native_state_open`, `test_late_native_match_after_acknowledgement_is_adopted`, `test_resolution_is_idempotent_and_conflicting_request_is_refused` |
| Scope violation | `test_lifecycle_and_resolution_api_enforce_scope`, `test_usage_report_api_scopes_project_and_institution`, `test_institution_scope_is_operator_only_and_includes_own_project` |
| Native pending vs rejection mapping | `test_native_wait_is_classified_without_conflating_rejection`, `test_native_rejection_is_terminal_with_proven_zero_allocation`, `test_only_allow_listed_native_refusals_are_rejections` (10 negative cases), `test_gateway_forwards_only_slurm_rejection_lines` |
| Monthly overlap / UTC boundaries | `test_monthly_report_clips_reservations_at_explicit_utc_boundaries` |
| Mixed units, clusters, not-finalized | `test_report_keeps_units_clusters_and_rejections_separate`, `test_period_report_lists_unresolved_attempts_as_not_finalized` |
| Trial runner budget and independent audit | `test_trial_plan_enforces_frozen_budget`, `test_trial_audit_detects_double_charge_and_keeps_unknown_unknown` |

## Review round 1 fixes (2026-10-29) — code + automated tests only

Fixed: unknown intervals stay in every period they could overlap; Kubernetes
status/reconcile/cancel bind attempt annotations + UID (recreated same-name Job is
never adopted); Slurm reconcile uses `--duplicates` + Submit and refuses distinct
(ID, Submit) records, with the matching gateway allow-list; the trial runner keeps a
whole-effort budget ledger bound to actual job limits and its audit requires
identities, settled observation and native receipts (unresolved never passes).

- `uv run pytest -q`: 1474 passed, 11 skipped, 3 failed: Kubernetes fixtures in
  `tests/test_operations.py` that lacked the ownership annotations every real Job
  carries. After the fixture fix, `tests/test_operations.py` passed (8).
- Python 3.11 (qualified runtime version) focused set: 194 passed.
- Each fix was first shown failing against the unfixed code.

## Review round 2 fixes (2026-10-29)

Kubernetes cancellation reads the Job, requires attempt annotations and a bound
UID (refused when no UID is bound), then deletes via `DeleteOptions.preconditions.uid`
with explicit propagation, so a same-name replacement is refused rather than
deleted. Targeted tests failed on the prior code and pass now. Full suite at
`4361066` on Python 3.11: 1477 passed, 11 PostgreSQL-only skipped; the two later
commits were checked with the related test files only (158 passed).

Remaining trial-runner defects (round 2, not fixed): an empty native receipt for
an unresolved attempt can still close a cohort; limits are checked after the
POST; per-key identities are persisted only at the end of `submit`. The runner
was **not used** live and must not be used until these are fixed.

## Live phase (2026-10-29) — deployment only

Evidence: [evidence/deployment-20261029.json](evidence/deployment-20261029.json).
Level: deployed to the owned lab and read back; **no new native workload ran**.

| Unit | Rollout | Readback |
|---|---|---|
| `ra-api` | direct image update, revision 96 (no Argo Application exists for it) | 1/1 ready, in-pod source tree matches build, Python 3.11.15 |
| `ra-live-training-worker` | last-applied manifest, image-only diff, Recreate | 1 pod, ready, 0 restarts, source matches |
| `ra-live-training-slurm-worker` | same | 1 pod, ready, 0 restarts, source matches |
| Slurm VM gateway | root-owned file replaced, previous kept as backup | SHA-256 `80d4d8be…` → `6db6728d…` |

Image source = reviewed commit `53cd73b` (108 files) plus 5 pre-existing untracked
modules preserved byte-for-byte from the previous deployment. It is not a pure
commit-exact image.

- API: `/healthz` 200; `/jobs/{id}/lifecycle` and `/usage/report?month=2026-10`
  return 200 (404 before); `/usage/summary` byte-identical to before.
- History: 876 terminal jobs and 876 usage rows hash identically before and after
  rollout; 878 jobs total. The two `live-k8s-cpu` `SUBMISSION_UNKNOWN` attempts from
  2026-10-28 stay open. They were not acknowledged and not reused as test jobs, and
  no reconciliation record had been written yet at readback (06:10Z).
- Backup before rollout: existing `backup_metadata.py`, captured but not
  restore-verified.
- **Native smoke: NOT_RUN.** The registered live-training workloads carry an
  immutable 900 s run limit, above the frozen 120 s/job bound (observed 10/28
  runtimes: Kubernetes 69–78 s, Slurm VM 148–165 s). New bindings were out of scope.
  Response-loss, restart and 10-job cohort trials were also not run.
  New native jobs: 0.
- **Incident:** while checking UID-precondition delete semantics, a
  `kubectl delete --raw …?dryRun=All` request was not a dry run. It deleted one
  owned, completed 2026-10-28 live-training Job (`a-4d76d3bd…`) and its Pod. Its DB
  job and usage rows are unchanged; the native object and Pod log are lost.
