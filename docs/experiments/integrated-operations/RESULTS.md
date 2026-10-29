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

## Live phase

Not run. To be filled only with real evidence per [PLAN.md](PLAN.md): exact
sanitized attempt/native IDs, observation boundaries, timestamps, raw evidence
checksums (`manifest.json` from the trial runner), independent recalculation
and the commands used. Failures and access blockers are recorded here as found.
