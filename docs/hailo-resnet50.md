# ResNet-v1-50 on Hailo-8

The single qualification prescribed by the [frozen plan](hailo-resnet50-plan.md)
passed all three gates on its disjoint 100-image subset. This qualifies this
specific compiled artifact, fixture and runtime, not arbitrary Hailo models.

| Measurement | Observed |
| --- | --- |
| NPU top-1 label accuracy | 80/100 |
| Original TFLite reference accuracy | 79/100 |
| Unrestricted NPU/reference top-1 agreement | 97/100 |
| Accuracy loss | −0.01; maximum allowed 0.05 |
| 100 synchronous inference calls | 0.339087234 s |
| Median / p95 call latency | 3.384091 / 3.421942 ms |
| Host process peak RSS | 128.15625 MiB |
| Scheduled → container finished allocation | 8 s × 1 NPU |
| Queue creation → admission | 0 s at scheduler timestamp resolution |

The 100 calls follow four warmups; transfers are within the synchronous host
float32 NHWC → NPU output call boundary. Download, reference calculation, image
build, container startup and result collection are outside that timing. NPU
memory, utilization and power remain unknown. The accuracy subset is class
balanced Imagenette, not a full ImageNet accuracy estimate. Earlier ResNet18 and
EfficientFormer failures remain unchanged and their causes remain unproven.

Evidence: [raw predictions](evidence/hailo-resnet50-qualification.json),
[input manifest](evidence/hailo-resnet50-inputs.json),
[scheduler provenance](evidence/hailo-resnet50-provenance.json).
HailoRT, PCIe driver and firmware are 4.23.0; Python 3.13.16, NumPy 2.2.6.
The manifest includes the original model and per-image tensor/reference hashes.

## Platform runner

[`hailo_benchmark.py`](../src/resource_advisor/hailo_benchmark.py) checks the
immutable image binding before device acquisition, then checks actual runtime
and result identity. The binding fixes the model, HEF, fixture, shape, seed,
precision, one physical NPU, one host CPU and 1 GiB host memory. The only allowed
modes are observation and fixed execution. No CPU inference fallback exists.

Every execution recomputes all three quality gates from predictions. A failed
agreement or accuracy-loss gate emits a bound `FAILED` result with **no usable
performance measurements**, even if accuracy alone passes. A successfully
emitted envelope gives process exit zero so the platform can collect and record
the failure. Configuration/runtime errors stop the process before any successful
result can be emitted.

`peak_memory_mib` is explicitly **host process RSS** for this workload; the
workload signature contains that memory boundary. It must not be compared to
CUDA allocated-memory measurements. Precision `hailo-compiled-float32-io`
describes the interface, without claiming uniform precision inside the HEF.

The standard result bundle carries identity and measurements. Full per-image
reports are emitted separately for archival and operator import into the
qualification registry; that import alone never grants execution rights.
The planned three observation API Jobs and one approved verification Job have
not yet been claimed as completed in this report.

## Edge log collection recovery

After the qualification finished, fetching its logs initially failed at the
edge fake-kubelet port. The four existing `cloud-iptables-manager` containers
were repeatedly OOM-killed with 50 MiB limits. They install the CloudStream DNAT
rules used by the [KubeEdge log/debug path](https://kubeedge.io/docs/advanced/debug/).

Only that DaemonSet's memory request/limit changed, from 25/50 MiB to 64/128 MiB.
CPU limits, images, CloudCore, node drivers and workload queue quotas were
preserved. The four replacement Pods became ready with zero restarts; observed
memory was 52–90 MiB. The **same completed qualification Pod's** logs were then
retrieved successfully. No qualification replay or replacement sample occurred.

Before applying such a repair, retain the original manifest, inspect node memory
headroom and confirm the actual last termination reason. Afterward verify
rollout, restart counts, resource use and retrieval from the original Pod. Keep
the rollback locally. These limits describe the measured lab repair, not a
universal production sizing recommendation.
