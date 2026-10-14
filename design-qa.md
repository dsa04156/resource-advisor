# Console workflow redesign QA

Status: passed for the scoped functional redesign (2026-10-14).

## Reference and comparison scope

This is an intentional adaptation of task-first patterns, not a pixel-identical clone. Official reference images and deployed implementation captures were placed side by side in a single comparison screenshot and visually inspected together. The private comparison artifact is `.state/resource-advisor/console-redesign-v1/reference-implementation-comparison.png` in the parent workspace. No reference product code was imported.

| Reference | Source capture | Implemented capture | Compared pattern |
|---|---|---|---|
| [ZenML onboarding (2024)](https://www.zenml.io/blog/oss-dashboard-release) | 3096×1912 PNG | 1440×1494, full page | Welcome → first steps → actions |
| [SkyPilot jobs](https://raw.githubusercontent.com/skypilot-org/skypilot/master/docs/source/images/dashboard-managed-jobs.png) | 3302×1158 PNG | 1440×900 | Search/status filters/table/detail actions |
| [dstack events](https://dstack.ai/static-assets/static-assets/images/dstack-ui-events-run.png) | 2560×1626 PNG, includes browser chrome | 1440×900 | Detail tabs and observed lifecycle records |

Implementation viewport was 1440×900 CSS pixels, screenshot scale `css`. Source CSS viewport/device scale cannot be inferred from public image resolution. Images were proportionally contained in equal-width comparison panels; spacing/font sizes were assessed from implementation CSS and original captures, not raw pixel ratios. Source data density varies (two SkyPilot jobs versus 25 paginated live HAIRP jobs). References were captured as published, not represented as their latest product version.

## Intentional changes

- Five primary sections and contextual tabs replace an infrastructure-heavy entry screen. GPUStack remains a resource/serving reference only; its model catalog does not define the main flow.
- Korean task wording, a 224px sidebar, white panels on a light-gray canvas and blue actions follow the local design tokens. dstack's dark theme is adapted to the same light shell.
- Task detail uses a dialog with four tabs, retaining the existing URL and native actions. Only observed events/times are shown.
- Tables use larger row spacing than the reference so status, target and actions are readable. Mobile forms stack into one column.
- Lucide library SVGs and the existing HAIRP mark are used. New assets include the source MIT license.

## Iterations and final checks

Initial shell capture exposed legacy CSS preventing the new grid from filling the page. Grid/width/margin overrides fixed it. Duplicate catalog/template cards were removed from the primary view. A clipped fifth mobile item was fixed with five equal navigation columns. Final references and implementation were then compared together again; no unresolved layout mismatch against this adaptation scope remains.

- Desktop home, catalog, jobs, detail, inference, resources, experiments and legacy destinations rendered. Home and submission have no document overflow.
- At 390×844, all five navigation items fit (55px high). The submit action is visible and 44px high; document width stays390px. [Mobile evidence](docs/images/console-workspace-mobile.png).
- Job status filter, search and next page returned matching live results. Four detail tabs and arrow-key navigation worked with six actual lifecycle records.
- Expanded advanced form, text, focus and caret survived a live refresh. Selected image and prediction survived refresh.
- A controlled 100MP PNG-header fixture was rejected with zero bitmap decodes. PNG/JPEG/WebP header tests passed.
- Two submit-handler invocations produced one intercepted POST; no native Job was created by this fixture. Normal submission still uses the existing API/idempotency contract.
- Actual image requests returned 200 through GPU before rollout and Hailo after rollout. The single-request timing is functional evidence, not a performance result.
- Deployed app console errors:0. The separate local comparison page's missing favicon is unrelated to the product.
- Focused API tests34/34, header tests3/3, Ruff, JS syntax and whitespace checks passed. Standards review: both findings fixed and rechecked. Spec review: all three findings fixed and rechecked.

## Deployment and limits

Release `665704562f024be1ef21b5da5f9fce09cfcc88f5` is Synced/Healthy in the existing Argo services Application with manual non-pruning sync. API/static source hashes match the built image. All1464 existing Job UIDs/specs, queue specs, existing serving resources and GPU/Hailo worker specs were preserved.

[Structured verification evidence](docs/evidence/console-workspace-20261014.json) distinguishes live requests, read-only navigation and intercepted UI fixtures. Full regression/load/long-running/native failure-injection checks remain deferred by the user's explicit preference. Existing routes were checked for rendering, not all native actions. This redesign does not claim arbitrary-model support, automatic retraining, high availability or a completed general MLOps lifecycle.
