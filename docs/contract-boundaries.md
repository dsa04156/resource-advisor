# Live E2/E3 contract boundary checks

The deployed HTTPS API correctly abstained from recommending a newly qualified
workload without performance history. It rejected unconsented/unbudgeted probes
and five deliberately incompatible descriptors before creating any API Job.
PostgreSQL and Kubernetes readbacks confirmed **zero new API Jobs, profiling
studies, usage records or scheduler objects during the API trial**.

This is boundary evidence, not a throughput comparison. The qualification step
used one actual RTX 5080 Job before the API snapshot: **2 physical GPU reservation
seconds**, measured from PodScheduled to container termination. Its result remains
outside the profile table. No claim of zero overall experiment cost is made.

## Qualification and positive control

The fresh contract uses the existing pinned PyTorch 2.8.0/CUDA 12.8 runtime and
generated three-convolution CNN, with seed 20261005, FP32, a 1×3×256×256 input,
three warmups and twelve blocks of 32 forwards. It requests one CPU core and one
physical GPU through the existing one-GPU Kueue queue. CPU-reference numerical
agreement was 1.0; this does not measure trained-model accuracy. The result digest,
model digest, phase timings and execution-bound NVML trace were checked before
registering the variant. No new worker execution binding was added.

A separate, already measured workload from the
[operational comparison](operational-comparison.md) served as the positive
control. Its recommendation contained twelve existing hardware profile references.
Those records were read back, with their identities and recording times preserved
in the capture. Thus the negative cases were not merely observed during a general
API, registry or history outage.

## Actual responses

| Case | Observed result |
| --- | --- |
| Qualified new model/input seed, no application profiles | `NEEDS_PROFILE`, empty ranking, no selected candidate |
| Attempt to approve that abstention | HTTP 422 |
| Direct pilot without a reserved study plan | HTTP 422, reserved-plan requirement |
| Study without profiling consent | HTTP 422, explicit consent required |
| Confirmation reserve too small | HTTP 422, independent confirmation budget required |
| Total budget entirely consumed by confirmation reserve | HTTP 422, no exploration budget |
| No device-specific budget | HTTP 422, `EXPLICIT_DEVICE_BUDGET_REQUIRED` |
| Changed input width | No compatible recommendation; HTTP 422 on submission; logical workload and shape mismatch |
| Changed batch and leading input dimension | No compatible recommendation; HTTP 422 on submission; logical workload and shape mismatch |
| Changed PyTorch version descriptor | No compatible recommendation; HTTP 422 on submission; runtime mismatch |
| Changed architecture descriptor | No compatible recommendation; HTTP 422 on submission; architecture mismatch |
| Changed precision | No compatible recommendation; HTTP 422 on submission; logical workload and precision mismatch |

The scope cases retain the old variant's actual qualification scope. A descriptive
alias does not qualify a changed runtime, model or input. Shape/batch/precision
changes create a different logical workload signature; runtime/architecture
changes create a different execution context signature. These were submitted as
negative descriptors; the incompatible programs were **not executed**.

The API trial preserved fingerprints of all non-entity database tables, including
Jobs, studies, outbox and usage. It also compared UID/spec fingerprints of
566 Jobs, 573 Pods, 566 Kueue Workloads, five Deployments, twelve Secrets and four
PVCs. Existing non-inventory registry entities outside the explicitly added
workload/variant/recommendation kinds were unchanged. The queue and outbox were
idle at completion. The new descriptors and rejection recommendations remain
auditable; no prior data was deleted.

## Recheck and reproduce

The [raw capture](evidence/contract-boundaries-v1.json) includes request bodies,
HTTP codes, rejection reasons, qualification result/trace, control profiles and
before/after fingerprints. Internal registry addresses, hostnames and credentials
are excluded. The API image digest is preserved separately. `source_commit`
identifies the parent revision during capture; the new case generator is additionally
identified by its source digest and committed with this report.

```bash
uv sync --locked --extra optimizer --extra artifacts --extra pipelines
uv run python examples/audit_contract_boundaries.py docs/evidence/contract-boundaries-v1.json
uv run pytest -q tests/test_contract_boundaries_report.py
```

The auditor validates the raw result digest/identity, phase/thermal evidence,
qualification cost, exact case design, registered contracts, request/response
associations, control provenance and preservation fingerprints. Mutation tests
reject misattributed responses, wrong denial causes, future/missing control data,
created Jobs, altered qualification and hidden costs. This offline recheck does
not itself contact the cluster or execute a new experiment.

To repeat the live trial on an authorized lab, first qualify a fresh cooperative
workload and keep its F0 result out of application history. Use
[contract_boundary_cases.py](../examples/contract_boundary_cases.py) to generate
the descriptors from that contract and a measured control. Register variants
with the operator identity and workloads with the project identity; submit the
requests listed in the capture to `/api/v1/compute`. Use fresh case references
and idempotency keys, preserve every response, and compare full database and
scheduler readbacks before claiming no execution. Never register an unmeasured
variant as model-verified or submit a supposedly invalid request without a bounded
lab scope. Credentials belong in the private operator configuration.

## Limits

This closes the bounded Kubernetes E2/E3 live acceptance cases and contributes an
architecture/precision negative case to E0. It does not qualify changed inputs or
runtimes, prove generic model support, run a Slurm/NPU workload, demonstrate live
performance drift, or complete the entire v0.3 project. Existing runtime behavior,
quotas, drivers and node configuration were preserved. The rejected requests use
the existing guards; this increment adds reusable cases and verified evidence.
