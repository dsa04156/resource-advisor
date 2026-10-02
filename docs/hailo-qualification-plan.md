# Hailo inference qualification plan

Frozen before reference/model inference, 2026-10-03 KST. This is a bounded
qualification of a specific inference variant, not a platform-wide NPU benchmark
or a claim about training support. Failures and preparatory allocations are retained.

- Runtime: ARM64, existing Hailo PCIe driver 4.23.0, isolated HailoRT 4.23.0;
  no host driver replacement. First discover Hailo-8 versus Hailo-8L through a
  bounded device-open Job admitted by a separate one-device Kueue queue.
- Model: public Hailo Model Zoo v2.17 ResNet-v1-18, original ONNX archive
  `bc5776177ddad2b43f36c218ae06124b3dd67caaed9dd80c906c507f0c02a22f`;
  select the official HEF matching the observed device architecture. Record
  both model hashes; do not silently substitute another model or CPU execution.
- Inputs: Imagenette2-160 validation split, source archive SHA-256
  `64d0c4859f35a461889e0147755a999a48b49bf38a7e0f9bd27003f10db02fe5`.
  Take ten images per class, selecting the smallest SHA-256 values of
  `20261003:<archive path>` within each class. Sort selected paths for execution.
  No output-dependent selection or substitution of difficult inputs.
- Preprocessing: RGB; resize the shorter edge to 256 with Pillow bilinear,
  center crop 224×224; freeze exact uint8 NHWC tensor bytes. Original ONNX
  reference uses those same tensors, transposed to NCHW and normalized with
  mean `[123.675,116.28,103.53]`, standard deviation `[58.395,57.12,57.375]`.
  The HEF embeds this normalization; verify its declared IO before execution.
- Labels: retain original 1,000-class ImageNet output indices; do not restrict
  predictions to the ten Imagenette classes. Fixed synset mapping is recorded
  with input paths and hashes.
- Gates: top-1 label accuracy at least 0.75 on the fixed 100-image sample;
  top-1 agreement with the original ONNX reference at least 0.90; no more than
  0.05 absolute accuracy loss versus that reference. A failed gate is retained
  and cannot qualify this variant for recommendations.
- Measurement: four warmup images followed by all 100 measured images,
  batch one. Time synchronous host-tensor-to-NPU-output calls. Report loading,
  preprocessing and qualification costs separately; this boundary is not full
  request latency. Record host process peak RSS explicitly as host memory;
  do not invent NPU memory usage, utilization or energy when unmeasured.
- Repetition: device/runtime F0, then model qualification, then three independent
  platform Jobs only if qualification passes. Each model Job requests one NPU,
  one host CPU and at most 1 GiB host memory, with a 180-second execution cap.
  Keep existing GPU quota and runtime unchanged.
- Evidence: retain Kueue admission/Pod allocation, exact image/runtime/model/data
  digests, predictions, reference predictions, labels, timing and failed attempts.
  Platform integration additionally requires validated API result, PostgreSQL
  usage, and matching S3/MLflow delivery; a direct qualification Job alone does
  not satisfy that integration gate.

The sample checks this named variant and dataset subset. It is not a statistically
powered estimate of full ImageNet accuracy, a new training evaluation, or a
fair performance comparison with another accelerator. Device reservation and
process isolation require separate negative checks; plugin registration alone
does not prove them.

Sources: [Hailo Model Zoo v2.17](https://github.com/hailo-ai/hailo_model_zoo/tree/v2.17),
[HailoRT v4.23.0](https://github.com/hailo-ai/hailort/tree/v4.23.0),
[Imagenette dataset](https://github.com/fastai/imagenette).
