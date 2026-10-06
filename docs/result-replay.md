# Live terminal-result replay and late-result rejection

Nine real HTTPS API requests passed the
[prospective protocol](result-replay-plan.md), committed before the trial.
[Sanitized evidence](evidence/result-replay-v1.json) retains HTTP causes, counts,
immutable result/artifact hashes and original reservation costs. The
[verifier](../examples/verify_result_replay.py) reads retained attempts and never
submits computation. Its executed source SHA-256 is in the evidence.

The positive control was an actual completed Slurm Orin numerical CNN attempt
from the two-project trial. The cancellation control was the earlier confirmed
[Slurm console cancellation](slurm-cancellation.md), with one resultless ledger
and KILLED MLflow run. The disconnected cohort's CANCEL_REQUESTED attempt was
not used as confirmed cancellation evidence.

| Actual request | HTTP | Required cause / behavior |
|---|---:|---|
| Original validated result and matching digest, twice | 200 / 200 | Original SUCCEEDED attempt, unchanged version |
| Positive result with another epoch | 409 | stale or unrelated attempt |
| Positive result with another attempt ID | 409 | stale or unrelated attempt |
| Terminal result with changed non-metric field and its digest | 409 | terminal result is immutable |
| Correct canceled identity, explicitly synthetic FAILED envelope, no measurements | 409 | terminal result is immutable |
| Same canceled negative with another epoch | 409 | stale or unrelated attempt |
| Other project's operator replays positive result | 404 | job not found |
| Owning ordinary researcher replays positive result | 403 | Operator qualification required |

Independent PostgreSQL snapshots confirmed identical target Job rows/versions,
immutable entities, usage and delivered outbox rows before/after. Global counts
of Jobs, usage, outbox, result and profile records were also unchanged. The owned
artifact bytes and API Job responses matched; actual MLflow REST searches found
exactly one unchanged original run for each attempt, FINISHED and KILLED
respectively. The canceled run still had no measurement metrics/result/profile/
artifact. Thus the rejection probe was not persisted as a measured observation.

The two original reservations were 73 and 27 GPU seconds, already accounted by
their original trials. No new GPU workload or reservation was created and these
historical costs are not charged again. The runner did not cancel, restart,
upgrade or change any compute infrastructure. Existing API ingestion guards
already enforced the observed behavior; this increment adds reusable verification
and live evidence rather than claiming a new production logic fix.

## Reproduce on retained lab records

Use an owning operator credential, its ordinary user credential, another
project's operator credential and explicit DB/MLflow read access from a private
configuration. Tokens and site addresses must remain outside Git. Require the
chosen success/canceled attempts to be terminal with one ledger each and all
target delivery events DONE. Do not run against an unresolved cancellation.

```sh
uv run python examples/verify_result_replay.py \
  --config /secure/result-replay-config.json \
  --report /secure/new-result-replay-report.json
```

The report path must be new. Stop and inspect saved receipts on any error; do
not create replacement compute or delete evidence. This is live API result
ingestion acceptance, not an independently delayed native collector, external
lease fencing, global exactly-once delivery or full E7 completion. The current
[disconnected Slurm cohort](slurm-project-isolation.md), physical Pi NPU and
other required compatibility/failure gates remain open.
