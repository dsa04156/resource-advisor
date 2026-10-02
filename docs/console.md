# Research resource console

Open `/console` on the Resource Advisor API origin. This is a new interface over
this project's own API and database; it does not embed or copy another platform's
application. The console ships as static assets inside the Python package and
needs no separate Node server or frontend build to run.

Connect with a project API bearer token. Use HTTPS beyond localhost. The token
is held only in this tab's module memory, cleared from the password field after
connection, and sent in the Authorization header. It is not stored in browser
storage, cookies, URL parameters or an asset. Refreshing or disconnecting clears
the session. A new authentication gateway is not implied by this small console.
Existing API credentials and project ownership checks remain authoritative.

## Four views

| View | Authoritative sources | Interpretation |
|---|---|---|
| 실행 현황 | Latest project inventory; own jobs and results | Measured CPU usage and memory availability beside scheduler request headroom; GPU/NPU units remain separate |
| 가속기 호환성 | Registered workload candidates, runtime variants and capabilities; existing compatibility function | Historical model validation can coexist with an expired capability; neither telemetry nor detection makes a candidate executable |
| 대기 · 할당 이력 | Project Kueue observations and terminal attempt ledger | Queue admission is reported separately from recorded reservations, actual compute and missing timing |
| 추천 근거 · 실제 결과 | Immutable recommendations, referenced results, approval-linked jobs and MLflow run references | Historical summaries are separate from a new independent, quality-passing hardware measurement |

The overview is `GET /api/v1/compute/overview`. Jobs, workload definitions,
terminal history and recommendations have independent zero-based page parameters
(`jobs_page`, `compatibility_page`, `history_page`, `recommendations_page`), each
with 25 rows and an explicit total. Workload rows include their candidate lists.
Job counters cover the whole authenticated project, not only the visible page.
Latest inventory selection is performed in SQL rather than loading all snapshots.
No command strings, backend credentials or object-store locations are added to
these read models. The UI is read-only: it does not submit, approve, migrate,
restart or cancel research workloads.

Expand a recommendation to call
`GET /api/v1/compute/recommendations/{ref}/evidence`. It shows candidate means,
descriptive intervals, individual evidence results, MLflow run IDs and additional
study costs when recorded. A historical mean is not a calibrated prediction.
A signed error is computed only for a later, approval-linked, independent,
quality-passing hardware result of the approved workload/configuration. Evidence
runs cannot be counted again as new validation. No new approved result means no
comparison, not zero error or a claimed improvement. At most the first 200 linked
executions are shown, with an explicit truncation flag.

For history-guided or RGPE recommendations, the same panel shows the frozen
source workloads and attempt IDs, independent target training IDs, initial
candidate order, stored mixture weights and fallback reasons. Warm start is
labeled separately from an ensemble fit. Weights are contributions to a model,
not accuracy or confidence. The source time covers only selected historical
Jobs; the panel keeps it separate from the new study's wall/planning cost and
does not imply that it includes the whole upstream search. These are stored
decisions: opening the panel never fits a GP or submits work. Up to 200 recent
plans are inspected, with a truncation flag when needed.
The [transfer preview check](evidence/transfer-console-preview.json) used the actual
completed first RGPE GPU study through a local read-only API, including two source
cohorts, 18 confirmation IDs and the recorded mixture weights. Desktop and 390px
mobile screenshots were inspected; no page overflow or browser errors occurred.
The running experiment's service deployment was not changed for this preview.

## Freshness and interaction

The queue view now retains Kueue condition reasons/messages, quota reservation,
admission checks and the reported priority class. Missing conditions and priority
are unknown. LocalQueue resource reservations and admitted usage are read only
within `queue_namespaces`. An explicit `cluster_queue_refs` allowlist enables
named ClusterQueue reads; the observer role grants `get` only on those named
objects. No cluster-wide queue listing is required.

ClusterQueue quota and counters cover all contributing namespaces, not an
individual project's exclusive allowance. Flavor/resource rows keep nominal
quota, reservation, admitted usage and borrowed reservation separate. They never
convert virtual units into physical GPUs or compute guaranteed availability from
quota subtraction. Missing usage is not zero; an expired snapshot hides the
whole queue observation. Field meanings follow the
[Kueue API](https://kueue.sigs.k8s.io/docs/reference/kueue.v1beta2/).
Resource reads are not an atomic scheduler snapshot. The
[live observer check](evidence/queue-inventory-preview.json) verifies named-object
RBAC, real quota/status reads, mobile rendering and hiding expired queue numbers.
It used a local preview; continuous service rollout is a separate check.

The default polling interval is 15 seconds. Each metric also retains its original
source timestamp, and browser-side expiration hides bars when their source TTL
passes, even while automatic refresh is off. Values become unknown when the
collector or API fails. A failed API refresh clears the old resource display.
Device utilization and scheduler reservations use different labels; unconfigured
Hailo utilization is explicitly unmeasured, not zero.

Backend polling now records `backend_observed_at` only after successful status
retrieval. Old job records without that field remain unknown; refreshing the web
page never upgrades them to current backend health. The console does not contact
Slurm or other services directly to infer health from old accounting records.
Opening detailed evidence pauses automatic refresh to keep the inspected content
stable. Re-enable the checkbox or use the explicit refresh button as needed.

Native keyboard controls and headings support navigation. At 390px width, wide
tables scroll within their own wrappers while the page fits the viewport. Data
strings are rendered with textContent; the same-origin Content Security Policy
allows no inline scripts, remote scripts or embedding. API/console responses use
no-store and nosniff. Disconnect aborts pending requests and discards the dataset.

## Verification and remaining gates

The backend tests cover cross-project isolation, authentication, pagination,
latest-snapshot selection, source failure, capability expiration, independent
comparison eligibility and successful/failed backend observation timestamps.
The full local suite passed 121 tests on SQLite and PostgreSQL; affected console
and worker tests were rerun after the comparison checks were tightened.

Real browser verification used the actual lab API/DB: ten inventoried nodes,
35 existing jobs and usage records, three workload definitions, four immutable
recommendations and their hardware evidence runs. There were no approval-linked
new executions for those recommendations, which the UI correctly shows as absent.
Browser fault injections test network failure, stale data and inert untrusted
text; these are UI fault tests, not hardware performance experiments.
See [verification evidence](evidence/console-verification.json).
For reproduction, open an isolated Playwright CLI session, connect with your own
project token, set a 390px viewport and run
`playwright-cli -s=console run-code --filename examples/console-browser-checks.js`.
The checks require a real inventory with at least one live meter, override API
responses only within that browser, and end by disconnecting the UI.

The console is not proof that all platform acceptance gates are complete. Slurm
live inventory, integrated quota utilization/priority reasons, pipeline service
health, durable supervision/token rotation and inventory retention remain open.
The [subsequent approved GPU demo](approved-gpu-demo.md) verified the live
approval→new-result comparison with independent hardware results. There is no invented GPU/NPU
normalization, per-job utilization attribution or NPU model qualification.

Execution results now include expandable [phase diagnostics](bottleneck-diagnostics.md).
These show possible bottlenecks and measured wall-time shares, with explicit missing
instrumentation and abstention states. They are not GPU utilization and do not
automatically change resource requests.
