# Network-aware JCT prediction (measured dataset access)

Opt-in extension of the heterogeneous recommendation (`hetero-rec-v1`) that replaces a
profile's separately bounded `data_staging` phase with **measured destination access to
the exact dataset object**. It reuses `JctProfile`, `RouteQueueSnapshot`, the scheduling
compiler, the Kubernetes/Slurm adapters, the immutable Store and the MLflow outbox; no
scheduler is replaced. Kueue/kube-scheduler/Slurm keep admission and node choice.

## What is new

| Piece | Where | Contract |
|---|---|---|
| `NetworkProfile` | `network_profile.py`, `POST /network-profiles` (operator) | Immutable, project-scoped. Workload/dataset version, `sha256` content digest, size, source endpoint + sanitized locator, backend/cluster/node (CLI: node = observer host; Kubernetes: built from RA job receipts whose Downward API Pod UID/node equal the adapter-observed Pod, `identity_source=ra_job_receipts`), observed cache directory identity (`device:inode`), cold/hot cache state, cache locator, `observed_at`, validity, 1–10 complete verified samples, limits. |
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

## Execution binding and identity (review rounds 1–2)

- **Cache binding.** `POST /cache-bindings` (operator) declares, per backend/cluster/node, the
  execution cache path and `persistent` (with observed `device:inode`) or `ephemeral` storage.
  Every measurement (cold or hot) must match the node's binding path (and identity when
  persistent) or the node abstains: `NETWORK_CACHE_BINDING_UNPROVEN` (no binding),
  `NETWORK_CACHE_BINDING_AMBIGUOUS`, `NETWORK_CACHE_BINDING_MISMATCH`.
- **Hot needs an attachment.** A hot read is admitted only where the cache persists into the
  job: a Slurm job on the node's own filesystem with a persistent binding. The Kubernetes
  adapter mounts no cache volume, so Kubernetes hot evidence is rejected
  (`NETWORK_CACHE_ATTACHMENT_UNSUPPORTED`) and the latest bound cold copy is used, or the node
  abstains. The plan carries the selected binding (`staging_bindings`, signed); the adapter
  passes it as `RA_STAGING_BINDINGS_JSON`, the staged workload stages into it, and ingest records
  `staging_binding_check` MATCHED/MISMATCH in the outcome.
- **Native identity.** Kubernetes Jobs receive `RA_NODE_NAME`/`RA_POD_NAME`/`RA_POD_UID` via the
  Downward API. `network_profile_from_receipts` and `jct_profile_from_jobs` accept a receipt only
  when its node and Pod UID equal the adapter's own Pod observation. The CLI refuses to run
  inside a Pod (`KUBERNETES_POD_REQUIRES_NATIVE_RECEIPT`); Kubernetes candidates use only
  receipt-derived evidence (`NETWORK_IDENTITY_NOT_NATIVE` otherwise).
- **Freshness.** Each receipt carries `staged_at` (end of staging, before compute). It must lie
  inside the job's native execution span (±5 s); stale samples are dropped and the profile's
  `observed_at` is the oldest kept sample, never a later job finish.
- **Total deadline.** HTTP reads are closed by a watchdog at the deadline (tolerance
  `DEADLINE_TOLERANCE_SECONDS` = 0.25 s), independent of buffering or socket activity.
- **Frozen comparison arms.** `POST /recommendation-comparisons/{ref}/arms/{arm}/jobs` submits the
  frozen plan digest (and, for policy arms, the frozen prediction); drift is rejected and
  recorded as an arm event (`DRIFT_REJECTED`), never reselected. All arms must share the same
  hard-eligible pool before Static is fixed (`COMPARISON_POOL_MISMATCH`).

## Runbook

```bash
cd resource-advisor
# 1. Destination measurement (run on the node that will run the job).
rtk proxy uv run --locked python -m resource_advisor.network_profile measure \
  --source https://storage.example/datasets/v1.bin --sha256 <hex> --size-bytes <n> \
  --cache-dir /var/cache/ra-datasets --mode cold --ref net-<node>-cold \
  --project <project> --workload-ref <workload> --dataset-version <version> \
  --source-endpoint-ref <endpoint> --backend slurm --cluster-ref <cluster> \
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

## Staged accelerator batch

`python -m resource_advisor.staged_accelerator_workload` is the accelerator counterpart of
`staged_workload`: the same dataset arguments, `RA_STAGING_BINDINGS_JSON`, Downward API
identity, `StagingReceipt` and `RESOURCE_ADVISOR_RESULT` envelope, but the staged bytes are
fed to a qualified native runtime instead of a CPU SHA-256 pass.

```sh
python -m resource_advisor.staged_accelerator_workload --runtime cuda|hailo|mobilint-candy \
  --workload-identity JSON [--config /opt/inference/model.json] [--fixture /opt/fixture] \
  --source URL --sha256 HEX --size-bytes N --source-endpoint-ref REF --cache-dir DIR
```

- `--workload-identity` (or `RA_WORKLOAD_IDENTITY_JSON`) is the accepted `WorkloadIdentity`; it
  must hash to `RA_WORKLOAD_SIGNATURE`, with `dataset_version = sha256:<staged digest>`,
  `input_shape` = the staged array shape, `work_units = RA_WORK_UNITS` and `model_digest` =
  `sha256:` + the source model the native runtime reports. It must also declare the semantics
  the executable performs: `task_type=inference`, `batch_size=1` (one tensor per native call,
  sequential), `global_batch_size=null`, `gradient_accumulation=1`, `measurement_boundary =
  staged-host-input-to-native-output-per-pass` and the runtime's qualified precision (`cuda`:
  `fp32`, `hailo`: `hailo-compiled-float32-io`, `mobilint-candy`: `mxq-float32-io`). A validly
  signed declaration of anything else is a different workload. Otherwise
  `WORKLOAD_CONTRACT_MISMATCH` before any transfer.
- Order: stage (≤32 MiB) → re-read and verify the staged bytes → `np.load(allow_pickle=False)`
  and shape/dtype/finite checks → native initialization (`inference_runtime.Runtime` with its
  fixed-100 qualification, or `npu_probe.mobilint(inputs=…, rounds=…)`) → runtime
  source-model identity check → `RA_WORK_UNITS` (1–20) timed passes over every staged tensor.
  `elapsed_seconds` is the passes only; staging is the receipt; initialization is residual
  preparation (also printed as `preparation_seconds` in `RESOURCE_ADVISOR_STAGED_ACCELERATOR_REPORT`).
- CUDA/Hailo ResNet50: input `(N,224,224,3)` float32 in [0,255]; every tensor must equal a
  qualified fixture sample (`tensor_sha256` in the manifest), so a staged object is a subset of
  `/opt/fixture/inputs.npy`. Quality is top-1 agreement with `reference_top1`.
- Mobilint ARIES2 Candy: input `(N,224,344,3)` float32; quality is output repeatability only
  (`runtime-repeatability-only`, `task_accuracy=null`). A different source model: never ranked
  against ResNet50. No same-source ResNet50 MXQ artifact is qualified.
- Any staging, input, identity, initialization or native inference failure is `FAILED` with an
  error code and no receipt, including an unreadable or malformed model configuration
  (`MODEL_CONFIG_INVALID`) or qualification manifest (`QUALIFICATION_MANIFEST_INVALID`).
  There is no CPU fallback.
- Image requirements: Python ≥3.11, `pydantic>=2.10,<3` (arch-matching `pydantic-core`), numpy,
  `httpx>=0.28,<1` with its dependencies (`httpcore`, `h11`, `anyio`, `idna`, `certifi`,
  `sniffio` where the anyio version needs it) for cold HTTP staging, the `resource_advisor`
  modules `contracts`, `network_profile`, `staged_workload`, `staged_accelerator_workload` and
  `inference_runtime` or `npu_probe` importable, `RA_RUNTIME_IMAGE_DIGEST` set for ResNet
  runtimes, and the qualified native SDK. `httpx` is imported lazily on the cold path, so an
  import-only check is not enough: validate a cold HTTP stage inside the actual image.
- `tests/test_staged_accelerator.py` drives real loopback HTTP bytes through runtime doubles: it
  proves protocol, consumed bytes and boundaries, not GPU/NPU performance.

## Evidence levels and limits

- Loopback and local-subprocess runs prove the byte/digest/timing, collector, ranking and
  outcome paths on one host. They are **not** remote Kubernetes/Slurm node, NIC or storage
  evidence, and a hot read includes the OS page cache.
- Running the staged workload through the real adapters needs a runtime image that contains
  `resource_advisor.staged_workload`, registered variant/capability records and a dataset
  endpoint reachable from the node. Results from a single host do not generalize to other
  nodes, contention levels or dataset sizes.
