# Prospective EfficientFormer-L1 qualification

Frozen before executing either reference or NPU inference for this model,
2026-10-03 KST. This is a new logical workload, not a replacement verdict for
the failed ResNet18 qualification. Do not tune until this sample passes or
report this as an accelerator speed comparison.

## Question and fixed contract

Can the official Hailo Model Zoo v2.17 EfficientFormer-L1 variant execute on the
existing Hailo-8, meet the same absolute/reference quality gates, and then enter
the platform's ordinary API/result/accounting path?

The model was selected from published architecture/artifact information before
observing its outputs. Its published full-precision ImageNet result is 79.13%;
that is a vendor/model-zoo figure, not a result of this project and not an
expected accuracy guarantee on our sample. Normalization matches ResNet18,
while the declared bicubic input resize and model architecture differ.

- Original ONNX archive SHA-256:
  `8372593b41a59f7e7d30ede776150c782bd3b7cb19073c30d9150c4826cdc8cf`.
- Official Hailo-8 compiled HEF SHA-256:
  `4ae331d8857308f452a671ae9e6a5f1825f23029e1ca49cc7e7d7a17a007953d`.
- Inputs: the same public Imagenette2-160 validation archive, SHA-256
  `64d0c4859f35a461889e0147755a999a48b49bf38a7e0f9bd27003f10db02fe5`.
  Exclude all 100 previously inspected ResNet18 sample paths, bound by previous
  manifest `b96a400c1dd303d76119f69af6c444d13faeccd8026998345f8d3056950c4239`.
  Within each of ten classes, take the ten smallest hashes of
  `20261003-efficientformer-l1:<archive path>` among the remaining images.
  Sort the resulting 100 paths for execution. No outcome-dependent replacements.
- RGB; Pillow 11.3.0 bicubic resize of shortest side to 256, center crop 224.
  Freeze uint8 NHWC tensor bytes and use identical tensors for both runtimes.
  ONNX reference: CPUExecutionProvider, float32 NCHW, mean
  `[123.675,116.28,103.53]`, std `[58.395,57.12,57.375]`.
  HEF input uses raw pixel values because normalization is embedded.
  This declared Pillow pipeline is not claimed bit-identical to the model
  zoo's TensorFlow implementation.
- Preserve the same zero-based 1,000-class labels and unrestricted argmax.
- Gates unchanged: accuracy ≥0.75, original-reference agreement ≥0.90,
  absolute accuracy loss ≤0.05. All must pass. A failure is terminal for this
  qualification; retain it without changing gates, sample or preprocessing.

## Execution and budgets

Reuse the verified HailoRT/driver/firmware 4.23.0 environment, its non-root
device-plugin allocation and dedicated queue. Use one Hailo device, one host
CPU, at most 1 GiB host memory and a 180-second per-Job cap, with zero retry.
Do not increase existing queue/GPU quota or change drivers.

One qualification Job: four warmups then 100 timed synchronous model calls.
Time the host-input-to-NPU-output boundary; record host RSS as host memory.
No NPU memory/utilization/power is inferred. Retain ONNX preparation/download/
image-building costs separately from device reservations, including failures.

Only after qualification passes, implement/register this exact variant and
execute three independent platform API Jobs under the same resource cap.
The platform Jobs must preserve the exact model, input and environment hashes,
produce validated result envelopes and match PostgreSQL usage, S3 artifacts
and MLflow runs. Registration alone is insufficient. These three Jobs measure
run-to-run variation, not 300 independent accuracy samples. No performance
improvement or model superiority claim is planned.

## Sources

- [Network configuration](https://github.com/hailo-ai/hailo_model_zoo/blob/v2.17/hailo_model_zoo/cfg/networks/efficientformer_l1.yaml)
- [Normalization/preprocessing](https://github.com/hailo-ai/hailo_model_zoo/blob/v2.17/hailo_model_zoo/cfg/base/efficientformer.yaml)
- [Compilation settings](https://github.com/hailo-ai/hailo_model_zoo/blob/v2.17/hailo_model_zoo/cfg/alls/generic/efficientformer_l1.alls)
- [Original ONNX archive](https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/Classification/efficientformer_l1/pretrained/2024-08-11/efficientformer_l1.zip)
- [Compiled Hailo-8 HEF](https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v2.17.0/hailo8/efficientformer_l1.hef)
