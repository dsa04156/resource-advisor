# GPU/NPU inference routing

The lab inference pool routes prepared image classification requests to persistent
CUDA and Hailo workers serving qualified variants of the same ResNet50 source.
Workers load and warm the model once. The router compares current in-flight demand
and recent service time, reserves its selected endpoint, and returns the actual
route alongside predictions. The existing linear model release/approval APIs are
unchanged; this pool does not promote a lifecycle model or bypass a protected release.

## API

Paths are relative to `/api/v1/compute`. Existing authentication and project ownership apply.

|Method/path|Behavior|
|---|---|
|`POST /inference-pools`|Operator registers an immutable pool of configured, READY endpoints.|
|`GET /inference-pools/{ref}`|User reads readiness, queue counts, timings and successful/failed attempts.|
|`POST /inference-pools/{ref}:predict`|User submits 1–4 prepared RGB images and receives predictions and routing metadata.|

Registration example:

```json
{
  "ref": "resnet50-lab-v1",
  "model_ref": "resnet50-lab-v1",
  "source_model_sha256": "2625772f5e1324c1e2781856926a445b4fe6494b9e0bb0161a7d86df7f095d38",
  "endpoint_refs": ["resnet-cuda", "resnet-hailo"]
}
```

Each `instances` row contains exactly 150,528 finite numbers in [0,255], flattening
RGB 224×224×3 in NHWC order. The CUDA adapter applies the qualified mean subtraction;
the Hailo adapter follows the qualified HEF input contract. Do not normalize again
in the client. Output has 1,000 scores per image and a `top1` class index. Scores
from distinct compiled variants need not be numerically identical.

`deadline_ms` defaults to 5,000 and accepts 50–10,000. The deadline starts inside the
router after request parsing and pool lookup. Health refresh, dispatch, fallback
and result checks share it; late results are rejected. It is not a client end-to-end
latency guarantee or hard socket cancellation. The JSON body needs Content-Length
and is limited to 12 MiB. Four image requests may enter one API process concurrently;
excess ingress returns 429 without buffering/decoding more image bodies.
Image upload must finish within five seconds; stalled uploads return 408 and release
their ingress slot before image parsing.

`routing` reports `endpoint_ref`, `runtime`, `estimated_ms`, `inflight_at_selection`,
`duration_ms`, `fallback`, and attempted endpoints/outcomes. Failed identity checks,
unavailable capacity and expired deadlines return 503. A failed endpoint has a
one-second cooldown; the router may try one other qualified endpoint within the same
deadline. A response lost after computation can result in duplicate read-only
inference during fallback. Raw image rows and scores are not persisted or logged.

## Endpoint configuration

`RA_INFERENCE_CONFIG_JSON` is server-side configuration with an `endpoints` map.
Each binding has `project_ref`, cluster-local Service `url`, `model_ref`,
`source_model_sha256`, `runtime` (`cuda`/`hailo`), `artifact_sha256`,
`runtime_image_digest` and `qualification_digest`. Input/output contract identifiers
are fixed to this prepared ResNet50 interface and participate in identity checks.
Users cannot supply destinations, credentials, images or executable model code.

Pool records bind the exact endpoint configuration digest. Changing an endpoint's
model, image or binding requires a new pool reference; an old pool fails closed.
Worker health and every prediction response must match all identity fields.
This is a trusted lab deployment contract, not cryptographic device attestation or
hostile-tenant network isolation. Native Services are cluster-local and tokenless;
API credentials and model-release operator credentials are not mounted into workers.

## Routing and operations

The score approximates waiting plus this request's service time:
`(max(remote_inflight, local_inflight)+1) × recent_ms_per_image × image_count`.
Using max avoids adding the same request twice; remote-only concurrent callers may
still make this estimate stale. Health cache lasts one second. Local selection and
reservation are atomic. Workers execute native inference serially with at most eight
in-flight requests and sixteen HTTP connections, bounded request/header reads,
startup/readiness/liveness probes, and a readiness gate after native failure.

Worker startup verifies artifact and fixed fixture hashes, runs warmup and the same
100 distinct images, and enforces accuracy≥.75, reference agreement≥.90 and at most
.05 reference-accuracy regression. READY means this startup check passed and the
worker currently reports ready; it does not prove ongoing accuracy on new traffic.

Standing workers request one physical accelerator each. Existing all-Pod inventory
counts their reservations when reporting device headroom. They are dedicated lab
Deployments, not Kueue batch Jobs; existing queue quotas remain unchanged. Direct
batch work requiring an occupied device can wait until that serving worker releases
it. There is no new global batch/serving quota or preemption controller.

## Current scope

The first runtime is prepared ResNet50 classification on the existing CUDA ONNX and
Hailo8 HEF variants. Pool records are durable; routing reservations, circuit state and
recent timings are API-process memory. One API process is the supported reservation
scope. Arbitrary model uploads, MLflow-to-NPU conversion, shared multi-replica
reservations, dynamic batching, canary release, and Console image upload are later work.
The default Console's existing linear model monitoring continues independently.

User-requested full load/failure/long-duration validation is deferred. Basic API checks
cover project/operator boundaries, queued GPU versus idle NPU, fallback, wrong model
responses, simultaneous reservations and late-result rejection. Existing serving and
release API checks cover compatibility. Native acceptance and selected routes are
recorded separately from these software checks.

Deployment: [dedicated worker renderer](../../deploy/inference-pool/README.md).
