# Resource Advisor console

Intent: researchers and platform operators inspect actual capacity, locate a queued
or failed attempt, and follow recommendation evidence before their next execution.
Inspection stays separate from explicit submission controls. Qualified candidate
rows offer an observe-run button with accelerator/CPU/memory/time limits. Pending
requests disable repeat clicks; uncertain responses reuse the same idempotency
key across refreshes without storing credentials. Active job rows offer explicit
cancellation; requested cancellation remains pending until backend termination is
observed. Terminal rows offer no cancellation control. Workload panels offer
recommendation creation without starting compute. Recommendation panels separate
evidence review, configuration approval and a later explicit execution action.
Abstained or expired recommendations cannot be approved. Approval references may
persist within the tab; credentials never do.

Domain: accelerator inventory, scheduler reservations, device sharing, source
freshness, runtime qualification, independent confirmation, allocation ledger.
Color world: equipment-room slate labels, pale instrument panels, teal healthy
signals, amber incomplete observations, red failed checks. Signature: each resource
row pairs measured utilization with scheduler reservations and a source-age label.
Rejected defaults: a summed heterogeneous GPU number, green zero for missing
telemetry, and giant equal-sized statistic cards without evidence provenance.

Hierarchy: match the user's mini-science-ai-os reference structurally, not just
with a dark hero. Fixed 244px sidebar contains the brand, primary navigation and
workspace identity; the sticky 90px topbar holds page title, refresh and new job.
The resource view orders fleet hero, project job-state flow, a joined KPI strip,
physical GPU/NPU observations alongside logical reservations, then full node cards.
The four-stage flow shows aggregate counts, not a fabricated per-job trace or an
unverified Kubeflow/MLflow health claim. Missing signals stay unknown.
Jobs use one search/filter/table panel beneath an execution heading; details and
cancellation retain the existing native dialog and scoped API. Search still runs
server-side across project records; node filtering remains local.
Palette follows the reference: neutral #f5f5f7 canvas, #1d1d1f text, #0071e3 actions,
#0b1716 rack, #102321 instrument and #58ddb2 signals. Cards use 22px radii, the
fleet hero 28px, a 4px spacing grid, subtle borders and a minimal panel shadow.
At mobile widths the six primary routes move to a scrollable bottom navigation bar; analysis
links remain accessible above the topbar, tables expose extra facts in details.
Compatibility and recommendation evidence remain secondary sidebar links.
An operator-selected submission catalog is the default; historical experimental
templates are opt-in. Expired capability evidence in operational mode is an amber
warning, never relabeled as freshly verified; execution and qualification are distinct.
Resources and jobs remain separate routes. Compatibility and evidence use dense comparison tables with
expandable provenance. Counts are secondary and name their scope.
Submission uses labeled native selectors for registered workload and execution
candidate, followed by a resource/time/priority summary and explicit submit action.
Selection persists across refreshes; focusing the form pauses automatic refresh.
Incompatible candidates remain inspectable with reasons and a disabled submit.
A secondary details panel imports WorkloadSpec JSON without starting compute.
Amber #805500 and red #a12e39 retain warning/error semantics. No remote fonts or assets.
Depth and layout use the reference tokens above; controls remain native. Typography: system sans, Korean fallbacks; 14px base, 12/14/18/24/30px
scale; 600 weight for values and tabular numbers; monospace for shortened IDs.
Spacing: 4px base; 16px panel padding, 24px section gaps, 36px minimum buttons.
Resource bars: native meter element, value and units always visible; unavailable
and stale states have text and no filled bar. Green never denotes qualification
unless the existing compatibility check passes for that specific candidate.
Native controls: links for navigation, buttons for actions, details for provenance,
labeled password input for authenticated installs; shared-lab mode auto-connects
to the server-selected project and hides login/logout. No credential is embedded
in assets. No custom keyboard widget.
Responsive: primary navigation becomes a bottom bar; tables keep local overflow wrappers;
page itself must fit a 390px viewport. No animated data transitions.

Slurm rows reuse the same table and meter components. Label the backend explicitly;
host CPU/memory meters use measured host totals, while reservation rows use Slurm
configured allocatable capacity. A fresh exporter must coexist with an amber
unknown scheduler badge. Unknown registration must never say no accelerators.
The history view separates Slurm account/partition record counts from Kueue
admission policy. Array-job records are not presented as expanded task counts.

Transfer evidence reuses the recommendation panel's tables and native details.
Lead with strategy and plain-language interpretation, then source provenance and
separate historical/target costs. Individual recorded choices expand to model
mixture weights and explicit fallback reasons. Percentages are labeled as mixture
contributions, never confidence. Warm start must not display invented RGPE weights.

Queue policy panels name their scope: LocalQueue namespace or ClusterQueue across
namespaces. Dense flavor/resource tables separate nominal quota, reservation and
admission; no available-GPU total is inferred from subtraction. Missing fields say
unreported, and stale observations hide the complete table. Use expandable native
details for controller conditions and admission-check messages.

Classification qualification imports live within Compatibility, using the existing
table/pager/details patterns. Separate inference completion from quality verdict;
show measured percentages beside their thresholds, and accuracy loss in percentage
points. Imported evidence has an amber provenance label. A passing imported report
uses a neutral badge, because import never authorizes a runtime candidate. The page
must retain both failures and an honest empty state without implying completion.

Template registration: the submission page has an explicit New Template action.
The inline two-column form collects a friendly name, registered workload and device
configuration, priority, run time and queue time. Resource/model/command values are
inherited and shown before saving. Saving selects the durable template; execution
requires the separate submit button. Collapse to one column on mobile. Draft text
survives dependent selector changes; reset clears project-specific draft state.

Operator-first home: Operations is the default route, with a compact shared-compute
header, joined state counters, backend-scoped queue cards, active-job table and
inspection list. Existing resource and job views stay separate. Usage has its own
route with current-project terminal accounting grouped by backend/model/device/mode,
CSV export and the existing ledger. Missing observations are amber, never an inferred
hardware outage; pending codes and suggested checks remain separate from diagnosis.

Workload-first scheduling: default submission leads with a workload and policy;
device pinning lives in an advanced disclosure. Preview exposes candidate checks
and the chosen route without reserving resources. A six-step responsive ordered
flow (six/three/two columns) uses blue completed observations and neutral future
steps, with explicit failure text. Job details and the latest submitted job reuse
this flow and an expandable candidate decision table. Unknown capacity is text,
never fabricated green headroom. Actual backend timestamps determine progress.

Research operations extension: HAIRP names the unified workspace. A three-service
connection strip links experiments, workflows and notebooks; unknown/unavailable
stays amber. MLflow rows support four-run selection and a comparison table above
results. KFP shows actual task states and dependency labels, plus linked compute
jobs. Notebook cards expose resource requests, readiness and explicit controller
actions; stop requires an inline second click. Reuse existing native tables,
dialogs and blue action tokens. Research navigation wraps on narrow viewports.
References and scope: docs/research-console.md.

Queue walkthrough: four request/queue/execution/terminal lanes display real job
cards, with selected-request inspector and a compact observation replay slider.
Resource request summary precedes explicit two-job launch. A/B are request labels,
not fabricated user identities. Replay is marked separately from live data;
observation times and native reasons are retained. Board uses four desktop columns,
two narrow columns; inspector and form collapse to one column. No automatic motion
of cards: explicit recorded-event playback is user controlled and can pause.

ML job graph: jobs default to an execution-lineage canvas with a horizontally
scrollable job picker; the table remains a toggle. SVG connectors join native
HTML buttons for model/input, workload, policy/queue, compute and result. These
are lineage links, not invented pipeline tasks. Only KFP's recorded dependentTasks
become DAG edges. A selected-node inspector exposes metadata and native actions.
Canvas supports zoom, fit, pointer pan, keyboard-accessible node buttons; narrow
screens start around the selected node instead of shrinking labels to illegibility.
Green denotes observed completion, amber wait, blue active/selected, gray configured
or unobserved. Existing text always accompanies color. No decorative animation.

Native scheduler lab: three explicit policy selectors lead to one real launch
button. A navy observation header anchors a left-to-right queue → native scheduler
→ physical GPU allocation board. Job cards and assigned worker chips select the
same evidence inspector. Recorded events have a seek slider; replay and latest
observations are labelled separately. The 3-column board stacks vertically below
760px. Slots show this experiment allocation only. Dark header is reserved for
native experiment state; amber wait, blue active and teal complete accompany text.

Multi-GPU PoC: count-only request control, four observation counters (request,
native admission, CUDA readiness, completed verification), and one clickable card
per worker. The three worker cards show actual CUDA model and architecture and
select worker evidence. Waiting workers keep readable empty states. On mobile,
counters use two columns and workers stack; configured physical capacity is never
presented as current fleet availability or a performance speedup.

Native timeline playback: completed history auto-plays once on initial open or
explicit history selection; running experiments remain live. Playback uses saved
snapshots every two seconds at 1×, labelled compressed observation intervals.
Pause/resume, restart, 0.5/1/2/4×, seek, and latest-record controls are explicit.
Seek and event clicks pause. Playback stops at the end, on page navigation, on
project reset and when the browser tab becomes hidden. No new workload is submitted.

Live allocation board: compact scenario tabs precede a horizontal current-resource
rail, then four observed-state lanes (queued, admitted/preparing, running, terminal).
Resource cards pair reservations/capacity with actual utilization and CPU/memory
meters; unavailable observations remain unknown. The rail stays current during
history playback and explicitly shows its own timestamp. Card selection highlights
recorded execution nodes and opens evidence; routing targets before execution do
not count as observed allocations. Slot/quota totals are never summed across device
types. Resource filter and horizontal scroll persist across refreshes.

This board supersedes the earlier no-motion rule only for scheduler-lab state
transitions: keyed cards move between observed lanes using 480ms transform/opacity
FLIP animation, with 120ms opacity-only reduced motion. Same-state polling does not
animate. Current presentation positions are captured before replacing the DOM so
replay seeking interrupts cleanly. Four columns become two below 760px. Keep all
actions keyboard-operable; retain focus on selected job/resource after refresh.
References: Run:ai allocation/utilization drilldown, KueueViz queue observations,
Slurm-web node/queue operations. No third-party scheduler/UI dependency was added.
