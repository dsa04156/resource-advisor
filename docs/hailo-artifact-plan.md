# E0: reject a different compiled NPU model

Freeze before compute. Use the already qualified Hailo-8 ResNet-v1-50 image,
binding, 100-image fixture and unchanged runner, on its current Ready/no-pressure
node through the existing Kueue LocalQueue. Preserve drivers, firmware, queue,
quotas, services and existing application records. Require idle compute.

Run exactly two sequential Jobs: one correct ResNet50 HEF control and one
different, previously executed ResNet18 HEF. No replacement Jobs or retries.
Both use the same digest-pinned main image and a digest-pinned, non-device
init container that copies the retained ResNet18 file to a new per-Pod emptyDir.
The main container mounts that directory read-only. Main requests one CPU,
1GiB and one physical `hailo.ai/h8`; init requests one CPU/256MiB and no NPU.
No service-account token, privilege escalation or hostPath; restart Never,
backoff zero, 90-second active deadline, initially suspended for Kueue admission.

The only runner input changed between arms is the HEF path. Both record actual
artifact, runner and binding digests before invoking the unchanged entrypoint.

- Positive: existing ResNet50 HEF SHA256
  `a1d82e9121c66e772257490cb3af904d1e90fb4387ad5f683fbc5efe1a05f9f7`.
  Require native success, validated result identity/digest, 100 hardware outputs,
  original accuracy/agreement/loss gates and qualified runtime versions.
- Negative: actual retained ResNet18 HEF SHA256
  `d5a76ed6f116fc9b9ef502df914a298917091724ed88cfccab36bfb716f61b22`.
  Require exit1, `compiled model digest mismatch`, no qualification/result marker
  and no usable performance output. Source control flow checks this digest
  before Device.scan/configuration/inference; no kernel tracing is claimed.

Retain original accepted IDs, Pod/init states, logs, Kueue conditions, resources
and timings. Report NPU reservation time for BOTH arms, including initialization
and the rejected arm, plus CPU core reservation time. Unknown timing stays
unknown, never zero. These direct qualification Jobs do not register API
profiles/results, MLflow runs or accounting rows; raw evidence carries costs.
Verify existing native object UIDs/specs and immutable application records stay
unchanged and the queue has no remaining test allocations. Stop on unexpected
availability, digest, outcome, eviction or timeout; inspect the same IDs.

This proves the specified compiled-artifact binding guard with a real different
HEF. It does not qualify ResNet18, arbitrary conversion, compiler correctness,
all NPUs, hostile tenants, Pi hardware or full E0/HAIRP completion.
