# EfficientFormer-L1: actual inference, reference agreement gate failed

The [prospective plan](hailo-efficientformer.md) was committed and pushed
as `3bb0ef8` before either original-reference or NPU inference. One actual
Hailo-8 Job completed four warmups and 100 measured image inferences. It exited
with code **2** because reference agreement missed the frozen gate.

| Quality criterion | Observed | Frozen minimum/limit | Verdict |
|---|---:|---:|---|
| NPU top-1 label accuracy | 77/100 | At least 75/100 | Pass |
| Original ONNX CPU reference accuracy | 81/100 | Reference measurement | — |
| NPU/reference top-1 agreement | 89/100 | At least 90/100 | **Fail** |
| Accuracy loss from reference | 0.04 | At most 0.05 | Pass |

All criteria are required. The variant remains unqualified, and the three
conditional platform API Jobs were **not submitted**. There is no model
registration, recommendation, PostgreSQL usage ingestion, S3 result delivery
or MLflow delivery claim for this direct qualification Job. No threshold,
input, label or precision was changed after output inspection.

## Evidence and costs

- [Raw result, all 100 predictions, hashes and timing](../evidence/hailo-efficientformer.json)
- [Input/reference manifest](../evidence/hailo-efficientformer-inputs.json)
- Hailo-8, HailoRT/PCIe driver/firmware 4.23.0; non-root existing device-plugin
  allocation through the dedicated Kueue queue, one NPU / one CPU / 1 GiB host
  memory. The node remained Ready without memory/disk/PID pressure.
- NPU reservation: **8 seconds** from Pod scheduling to container finish;
  **13 seconds** from admission to observed Workload finish. These are
  different boundaries, not physical utilization. The queue returned to zero
  pending/admitted/reserving workloads.
- Synchronous inference p50/p95/p99: **27.436 / 27.534 / 27.561 ms**;
  total 100-call time **2.743 seconds**; throughput **36.46 images/s** inside
  this boundary. Host RSS peak **95.81 MiB**; NPU memory/utilization/power remain
  unknown. Conversion, model loading, queueing and uploads are outside the
  timed inference boundary.
- Fixture preparation **5.177 seconds**, including **2.342 seconds** of CPU
  reference execution; image build/push **1.322 seconds**. Model/HEF downloads
  took **21.187 / 8.917 seconds** concurrently. These overlapping measurements
  must not be summed as total wall time; complete preparation cost remains
  unmeasured. CPU reference timing is not a cross-accelerator benchmark.

The 100 image paths have **zero overlap** with the previous ResNet18 sample.
Each class contributes ten images selected by the preregistered hash policy.
Model, inputs and interpolation differ from that earlier experiment: neither
accuracy nor latency differences establish model or device superiority. This
small sample is not full ImageNet validation or a powered population estimate.

The ResNet18 investigation found matching normalization constants and correct
class indices. The declared Pillow interpolation is not bit-identical to the
model zoo's TensorFlow preprocessing. No root cause for either model's failed
quality gate has been established, and these observations do not justify
blaming the device or claiming a quantization defect.

## Reproduction

Use the runtime/package setup in [Hailo qualification](hailo-resnet50.md),
the EfficientFormer asset URLs/hashes in the [plan](hailo-efficientformer.md),
and the isolated original-reference environment (NumPy 2.2.6, Pillow 11.3.0,
ONNX Runtime 1.22.1):

```sh
python examples/prepare_hailo_fixture.py \
  --recipe efficientformer-l1 \
  --model-archive efficientformer_l1.zip \
  --data-archive imagenette2-160.tgz \
  --exclude-manifest docs/evidence/hailo-input-manifest.json \
  --output efficientformer-fixture
```

The previous manifest's exact hash is required: missing or changed exclusion
evidence must not silently reuse previously inspected inputs. The original
ResNet18 recipe and its sample selection remain available unchanged by default.
New manifest bytes include preparation timings, so freeze their new digest.

Build a new immutable image layer containing the same qualification runner,
the new frozen fixture and EfficientFormer HEF. In the existing suspended
Job template set this image, the new manifest/HEF digests and 1 GiB host-memory
request/limit. Save complete Job/Pod/Workload/log evidence and retain any failed
attempt. Exit 2 with a complete report indicates a quality rejection, not a
failure to execute all images. Do not silently rerun or register this variant.

Official input contracts: [EfficientFormer configuration](https://github.com/hailo-ai/hailo_model_zoo/blob/v2.17/hailo_model_zoo/cfg/base/efficientformer.yaml),
[ResNet normalization](https://github.com/hailo-ai/hailo_model_zoo/blob/v2.17/hailo_model_zoo/cfg/base/resnet.yaml),
[reference preprocessing implementation](https://github.com/hailo-ai/hailo_model_zoo/blob/v2.17/hailo_model_zoo/core/preprocessing/classification_preprocessing.py).
