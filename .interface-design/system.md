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

Hierarchy: a dedicated job submission section precedes the four inspection sections.
The main navigation emphasizes submission, resources/jobs, and queue/usage.
Compatibility and recommendation evidence are grouped in native advanced details.
An operator-selected submission catalog is the default; historical experimental
templates are opt-in. Expired capability evidence in operational mode is an amber
warning, never relabeled as freshly verified; execution and qualification are distinct.
Execution starts with a prominent new-job link, then the node table,
then attempt history. Compatibility and evidence use dense comparison tables with
expandable provenance. Counts are secondary and name their scope.
Submission uses labeled native selectors for registered workload and execution
candidate, followed by a resource/time/priority summary and explicit submit action.
Selection persists across refreshes; focusing the form pauses automatic refresh.
Incompatible candidates remain inspectable with reasons and a disabled submit.
A secondary details panel imports WorkloadSpec JSON without starting compute.
Palette: slate text #192d3a on #f4f7f8, white panels, teal #006d69,
amber #805500, red #a12e39. No remote fonts or assets.
Depth: subtle borders only. Sidebar shares the canvas, panels are white, inputs
are inset #edf2f4. Typography: system sans, Korean fallbacks; 14px base, 12/14/18/24/30px
scale; 600 weight for values and tabular numbers; monospace for shortened IDs.
Spacing: 4px base; 16px panel padding, 24px section gaps, 36px minimum buttons.
Resource bars: native meter element, value and units always visible; unavailable
and stale states have text and no filled bar. Green never denotes qualification
unless the existing compatibility check passes for that specific candidate.
Native controls: links for navigation, buttons for actions, details for provenance,
labeled password input for authenticated installs; shared-lab mode auto-connects
to the server-selected project and hides login/logout. No credential is embedded
in assets. No custom keyboard widget.
Responsive: navigation becomes horizontal; tables keep local overflow wrappers;
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
