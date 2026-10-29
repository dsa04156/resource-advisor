# Network-aware JCT prediction (measured dataset access)

Opt-in extension of the heterogeneous recommendation (`hetero-rec-v1`) that replaces a
profile's separately bounded `data_staging` phase with **measured destination access to
the exact dataset object**. It reuses `JctProfile`, `RouteQueueSnapshot`, the scheduling
compiler, the Kubernetes/Slurm adapters, the immutable Store and the MLflow outbox; no
scheduler is replaced. Kueue/kube-scheduler/Slurm keep admission and node choice.

## What is new

| Piece | Where | Contract |
|---|---|---|
| `NetworkProfile` | `network_profile.py`, `POST /network-profiles` (operator) | Immutable, project-scoped. Workload/dataset version, `sha256` content digest, size, source endpoint + sanitized locator, backend/cluster/**node = observer host**, cold/hot cache state, cache locator, `observed_at`, validity, 1–10 complete verified samples, limits. |
| Measurement CLI | `python -m resource_advisor.network_profile measure` | Runs **on the destination**. Streams HTTP(S) or copies a local/mounted file into an explicit cache dir (`cold`), or reads a verified cache entry (`hot`). Monotonic clock, fsync, full SHA-256. |
| `StagingReceipt` | `staged_workload.py` → result envelope `staging_receipt` → `Service.ingest` | One attempt's own staging, timed apart from its measured compute span. |
| Collector | `jct_profile_from_jobs` | If every job has a verified receipt measured on its allocated node, `data_staging` becomes a separate phase (`staging_boundary="separate"`) and is subtracted from the residual. Otherwise staging stays `inside_compute` (unchanged). |
| Measured mode | `RecommendationPolicy(version="hetero-rec-v2", policy="profile_queue_network")` | Takes no topology snapshot. `v1` records keep their link-model meaning and digest. `DataLocation.content_digest` (optional, omitted from the digest when unset). |
| Cost breakdown | `recommendation_evidence.predictions[].breakdown` | queue wait, admission, preparation (`runtime_startup`+`model_preparation`), staging (source, cache state, samples, min/max, age, refs), compute, other, critical path, overlap groups, uncertainty. |
| Outcome | `jct_outcome` entity per attempt, `GET /jobs/{id}/recommendation` → `outcome` | Frozen prediction vs native JCT. |
| Comparison | `POST/GET /recommendation-comparisons`, `hetero_experiment.run_comparison` | Static / Profile Only / Profile + Queue / Profile + Queue + Network. |
| MLflow | `MLflowDelivery.deliver_one` | `recommendation.*` tags; metrics only for an `OBSERVED` outcome. |
| Portal | Job detail → **결과** tab → "추천 근거 · 예측과 실제" | Existing styles; loading/error/no-policy/not-finished states. |

## Rules that keep the estimate honest

- **Nothing assumed.** No link bandwidth, no cache hit, and same-host access is not zero
  unless measured. A node's latest fresh measurement (cold or hot) is used; pools use the
  slowest member.
- **Only complete evidence.** A partial, truncated, over-size, timed-out or digest-mismatched
  read is `UNAVAILABLE` with reasons (`TRANSFER_INCOMPLETE`, `CONTENT_DIGEST_MISMATCH`,
  `SIZE_EXCEEDS_LIMIT`, `SIZE_MISMATCH`, `TRANSFER_TIMEOUT`, `SOURCE_UNREACHABLE`,
  `SOURCE_HTTP_<code>`) and produces no profile. Caps: ≤1 GiB, ≤120 s, ≤10 repeats.
- **Cache safety.** The cold copy writes a private `.partial-*` file, verifies it, and
  installs it with `link()` (never replaces an existing entry); its own temp file is always
  removed. A corrupt shared entry is reported as `CACHE_CORRUPT` and **left in place**.
- **No localhost-as-remote.** `node_ref` must equal the measuring host's name
  (`OBSERVER_NOT_TARGET` otherwise); the contract rejects any other pairing.
- **No secrets.** User info, query and fragment are stripped from the stored locator.
- **Unknown stays unknown.** `NETWORK_PROFILE_MISSING` (incl. another project's evidence,
  other endpoint or dataset version), `NETWORK_PROFILE_STALE`, `NETWORK_PROFILE_FROM_FUTURE`,
  `NETWORK_DATASET_MISMATCH` (digest/size), `DATA_DIGEST_UNKNOWN`, `STAGING_NOT_SEPARABLE`.
- **No double count.** The measured time *replaces* `data_staging`; a profile whose staging
  is inside compute abstains.
- **Uncertainty is descriptive.** `calibrated: false`; profile stddev and staging min/max
  with sample counts. Single samples and point queue/admission estimates are listed in
  `uncertainty.missing` (`staging_spread`, `profile_spread`, `queue_wait_spread`,
  `admission_spread`); there is no 95 % claim and never zero uncertainty.

## Outcome record

On a terminal transition of a scheduled job the Store writes `jct_outcome/<attempt>` in the
same transaction: policy/version/status, candidate, plan digest and reasons, the prediction
frozen in the accepted plan (`evaluated_at` precedes submission), and the actual span
`scheduler_submitted_at → backend_finished_at`. Error = actual − predicted (signed,
absolute, relative to actual). `FAILED`/`CANCELED`/`RESULT_INVALID` → `NOT_COMPLETED`;
missing native timestamps → `TIMESTAMPS_MISSING`; neither gets a JCT or error. MLflow
receives it through the existing durable outbox (same attempt-tagged run, terminal
timestamp, retry on failure); an MLflow outage never changes the job or the record.

## Comparison

`POST /recommendation-comparisons` evaluates all four arms for one workload in one
transaction and stores them immutably (`kind: shadow_estimates_frozen_before_submission`).
Static must be an eligible candidate under a profile without recommendation; each policy
arm must use a profile of that policy. Re-posting the same request returns the stored record;
a different request under the same ref is a conflict. `run_comparison(..., execute=True)`
submits one job per arm with idempotency key `cmp:<ref>:<arm>` and settles each before the
next, so no arm's job is queued demand in another arm's estimate. `GET` lists those jobs as
`independently_executed` with `matches_frozen_selection` and their outcomes. Shadow estimates
are never presented as measured performance of an arm that was not executed.

## Runbook

```bash
cd resource-advisor
# 1. Destination measurement (run on the node that will run the job).
rtk proxy uv run --locked python -m resource_advisor.network_profile measure \
  --source https://storage.example/datasets/v1.bin --sha256 <hex> --size-bytes <n> \
  --cache-dir /var/cache/ra-datasets --mode cold --ref net-<node>-cold \
  --project <project> --workload-ref <workload> --dataset-version <version> \
  --source-endpoint-ref <endpoint> --backend kubernetes --cluster-ref <cluster> \
  --repeats 3 --max-bytes <cap> --timeout-seconds 60 --out net.json
#    then POST net.json to /api/v1/compute/network-profiles as an operator.
# 2. Declare DataLocation with content_digest; profile jobs with the staged workload
#    (python -m resource_advisor.staged_workload, RA_DATASET_* env) and build the profile
#    with jct_profile_from_jobs; import it to /jct-profiles.
# 3. Scheduling profile: {"recommendation": {"version": "hetero-rec-v2",
#    "policy": "profile_queue_network"}} plus fresh /route-queue-snapshots.
# 4. Compare: hetero_experiment.run_comparison(client, request, execute=True).

# Local actual-byte demonstration (loopback only, writes DIR/evidence.json):
rtk proxy uv run --locked python examples/network_jct_demo.py --out /tmp/nj-demo --mib 16
# Local end-to-end (real subprocess + bytes, frozen prediction → JCT):
rtk proxy uv run --locked pytest -q tests/test_staged_execution.py
```

## Evidence levels and limits

- Loopback and local-subprocess runs prove the byte/digest/timing, collector, ranking and
  outcome paths on one host. They are **not** remote Kubernetes/Slurm node, NIC or storage
  evidence, and a hot read includes the OS page cache.
- Running the staged workload through the real adapters needs a runtime image that contains
  `resource_advisor.staged_workload`, registered variant/capability records and a dataset
  endpoint reachable from the node. Results from a single host do not generalize to other
  nodes, contention levels or dataset sizes.
