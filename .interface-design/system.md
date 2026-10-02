# Resource Advisor console

Intent: researchers and platform operators inspect actual capacity, locate a queued
or failed attempt, and follow recommendation evidence before their next execution.
Read-only inspection stays separate from submission/approval API operations.

Domain: accelerator inventory, scheduler reservations, device sharing, source
freshness, runtime qualification, independent confirmation, allocation ledger.
Color world: equipment-room slate labels, pale instrument panels, teal healthy
signals, amber incomplete observations, red failed checks. Signature: each resource
row pairs measured utilization with scheduler reservations and a source-age label.
Rejected defaults: a summed heterogeneous GPU number, green zero for missing
telemetry, and giant equal-sized statistic cards without evidence provenance.

Hierarchy: four compact navigation sections; execution starts with the node table,
then attempt history. Compatibility and evidence use dense comparison tables with
expandable provenance. Counts are secondary and name their scope.
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
labeled password input for project token, no custom keyboard widget.
Responsive: navigation becomes horizontal; tables keep local overflow wrappers;
page itself must fit a 390px viewport. No animated data transitions.

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
