# Classification qualification records

Operators can import bounded, completed classification experiments into the
platform without relabeling them as platform-submitted Jobs. The server stores
immutable project-owned evidence in the existing PostgreSQL entity store and
recomputes accuracy, original-reference agreement and accuracy loss from each
image's label and two predictions. A quality failure is retained as evidence.

The console's **가속기 호환성 → 모델 검증 이력** table separates completed inference
from quality acceptance, shows each measured value next to its threshold, and
labels provenance as an external operator import. A report does not establish
current node readiness or authorize a RuntimeVariant.

## API and authority

| Endpoint | Permission | Effect |
|---|---|---|
| `POST /api/v1/compute/qualifications` | Project operator | Validate and persist an immutable report; recompute gates |
| `GET /api/v1/compute/qualifications?page=0` | Project reader | 25 summaries per page, no per-image arrays |
| `GET /api/v1/compute/qualifications/{ref}` | Owning project reader | Exact retained per-image records and assessment |
| `GET /api/v1/compute/overview?qualifications_page=0` | Project reader | Same bounded summaries for the existing console |

The authenticated principal determines the project; a differing `project_ref`
is rejected. Readers cannot import reports, and another project receives 404
for a specific record. An identical import returns the existing record. Reusing
its reference with different contents returns a conflict rather than overwriting
the failure. Requests cannot provide a precomputed assessment or a pass flag.

Contracts require 1–1,000 distinct input tensor digests and exactly one prediction
per expected image, finite positive timings, bounded 1,000-class integer indices,
explicit thresholds, model/input/image/runner/report digests and ordered execution
timestamps. Future-dated evidence is rejected. Missing predictions, duplicate
inputs, NaN values and fabricated extra assessment fields fail validation.

## Provenance and limitations

`provenance=operator_import` means the operator attests to an external execution.
The server checks the submitted structure and recomputes metrics; it does not
contact the scheduler or cryptographically attest the accelerator. A digest
identifies the operator's referenced artifact, but is not independent proof of
that artifact's origin. Hardware and synthetic records remain explicitly labeled.

The import creates no scheduler Job, usage entry, recommendation, capability,
RuntimeVariant, MLflow run or S3 object. Historical reservation intervals are
shown as source evidence and are not added to the normal usage ledger, preventing
double counting. Imports alone do not satisfy NPU platform-submission or artifact
delivery acceptance. The later [ResNet-50 integration](hailo-resnet50.md) separately
verified four actual API Jobs and imported their full classification evidence.
Ordinary execution and recommendation eligibility continue to use their existing
verification checks.

An inference report can contain all expected outputs while the external
container exits 2 to reject quality. The console preserves that exit code and
the failed gate instead of describing an inference crash or a qualified model.
A later experiment needs a new reference; it cannot erase a previous rejection.

## Hailo archive importer

`examples/hailo/build_import.py` renders a request from archived report, input
manifest, complete log, image build manifest, Job, Pod and Kueue Workload captures.
It checks exact log/report equality, fixture/model hashes, per-image bindings,
Job ownership of Pod/Workload, actual image digest, Hailo request, recomputed
verdict and matching exit code. It performs no network fetch or API write.

```sh
uv run python examples/hailo/build_import.py \
  --report report.json --manifest fixture/manifest.json --log workload.log \
  --job job.json --pods pods.json --workload workload.json \
  --image-report image-build-report.json \
  --project research-a --ref hailo-model-check-001 \
  --model-name model-name --plan-ref frozen-plan-001 > import.json
```

Use an existing project operator credential through your authenticated API
client to POST the generated JSON. Keep credentials and site-specific captures
outside the public repository. The Hailo importer uses the two published plans'
fixed 0.75/0.90/0.05 gates; it is not a general model conversion or tuning tool.

Tests cover permission boundaries, conflicting retries, lack of execution/usage
side effects, rejection of incomplete and forged reports, and bounded pagination.
The browser check covers failed-gate visibility, actual archived metric values,
provenance, mobile overflow, literal rendering of HTML-like model names and empty
state behavior. CI tests and imported archives are not new hardware measurements.

## Verified lab increment

The two published Hailo trials were imported through the deployed HTTPS API and
read back from PostgreSQL with matching record digests and per-image predictions.
Repeated imports were identical and reader imports returned 403. Job, usage,
outbox, variant, capability and recommendation counts did not change.

The deployed console asset matched the source hash. Chromium verified the live
records through a loopback read-only proxy that validates the lab HTTPS
certificate; direct Chromium trust of that certificate was not established.
The browser checks covered failed-gate labels, metric values, provenance, mobile
width, escaped model text and empty history. The last two were injected UI cases.
See [sanitized verification evidence](../evidence/qualification-registry.json).

During rollout, the old Pod's port-forward lost its target. The existing systemd
restart policy reconnected to the replacement Pod, after which HTTPS checks and
imports succeeded. This lab tunnel is not evidence of production ingress or HA.
