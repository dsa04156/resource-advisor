# E0 CUDA fallback refusal: bounded live protocol

Freeze before two new lab Jobs. Reuse the existing digest-pinned CUDA image,
read-only qualified runtime PVC and source ConfigMap on the qualified NVIDIA
server. Do not change its driver, runtime libraries, sources, queue, quota or
static services. Require current Ready/no-pressure node, Bound PVC and an idle
Kubernetes compute queue. The unrelated disconnected Slurm attempt is preserved.

Bound is one fresh positive Job and one negative Job, sequentially, no retries
or replacement compute. Both begin suspended and are admitted by the existing
Kueue LocalQueue; never manually unsuspend. Request one CPU/2GiB, no API token
or service-account mount, restart Never, backoff zero and 90s active deadline.

Use the actual `resource_advisor.gpu_benchmark` matmul runner: FP32 256×256,
seed0, three warmups, 20 measured iterations. A thin identical wrapper prints
actual Torch/CUDA versions, CUDA availability and visible-device count before
running the unchanged module. Its context validates Torch/CUDA versions; this
runner does not validate driver version, so driver metadata is not passed as
an unsupported field or claimed verified by that module.

- Positive: one physical NVIDIA GPU request, normal qualified visibility.
  Require actual CUDA available/one device, exact pinned versions, correct
  identity/result digest, numerical agreement1.0 and native Job COMPLETED.
- Negative: omit GPU resource request; set NVIDIA_VISIBLE_DEVICES=void and
  CUDA_VISIBLE_DEVICES empty, leaving image/source/runtime/input unchanged.
  Require actual CUDA unavailable/zero devices, identical Torch/CUDA versions,
  exit1 and `CUDA unavailable; refusing CPU fallback`. No result marker or
  performance/profile data may be emitted. This is explicit visibility loss;
  it is not proof of hostile-user device isolation without those settings.

Retain intent, accepted Job/Pod UIDs, Kueue conditions and before/after Pod
resources, timestamps, logs and image/source digests. Charge all actual GPU
reservation and CPU core reservation, including failed attempts; never turn
unknown timing into zero. These direct qualification Jobs stay outside API
execution/profile history. Independently verify API Job/profile/result/usage
counts are unchanged, old metadata remains intact, existing Kubernetes objects
and source/runtime references are preserved, and no test allocation remains.
Publish sanitized measurements/denial evidence and retain private manifests.

Stop on any unexpected availability, result, error cause, eviction or timeout.
Inspect the same accepted IDs; do not replay until a convenient success.
This closes only the named CUDA fallback gate. Other model conversion failures,
all-accelerator support, physical Pi NPU and complete platform acceptance remain
separate requirements.
