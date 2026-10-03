# Prospective ResNet-v1-50 qualification and conditional integration

Status: frozen before reference/model inference. This is one additional, separately
identified model qualification, not a replacement for the failed ResNet18 or
EfficientFormer verdicts. Neither preceding failure has an established root cause.
The original/reference and NPU both used identical input tensors; the documented
Pillow/TensorFlow difference alone does not prove why either gate failed.

## Fixed scope

Choose the official Model Zoo v2.17 `resnet_v1_50` CNN based on its documented
architecture and available reference/compiled artifacts. No output of this model
was inspected when selecting it. Its original is **TFLite**, with float32 NHWC
224×224×3 input and a 1,000-class softmax output. Static graph inspection found
PAD followed by convolution, with no input normalization operator before PAD.
Unlike the previous PyTorch variants, subtract mean `[123.68,116.78,103.94]`,
with unit standard deviations, for the reference. The HEF embeds this same
normalization and receives raw float32 pixels. No BGR swap or label restriction.

Freeze these public assets:

| Asset | SHA-256 |
| --- | --- |
| Original `resnet_v1_50.zip` (2025-01-15) | `33eaf6896f9fb4876375d53eb12e32a90a12055d86c541f76361817d3aa8d5ad` |
| Hailo-8 v2.17 `resnet_v1_50.hef` | `a1d82e9121c66e772257490cb3af904d1e90fb4387ad5f683fbc5efe1a05f9f7` |
| Imagenette2-160 validation archive | `64d0c4859f35a461889e0147755a999a48b49bf38a7e0f9bd27003f10db02fe5` |

Use 100 new validation images: exclude all 200 paths bound by the exact two prior
manifests (digests in the preparation script), then choose the ten lowest
SHA-256 hashes of `20261005-resnet-v1-50:<path>` in each of ten classes.
Processing order uses seed 20261005 and the experimental-design skill's
`assign_factorial_runs` permutation. Both reference and NPU use that same order; the exact
[100-image schedule](evidence/hailo-resnet50-sample-plan.json) is frozen alongside
this plan.
There are no output-dependent replacements or additional qualification attempts
after a complete quality verdict. All failures and costs remain visible.

Decode RGB with TensorFlow 2.20.0, resize the shortest side to 256 using bilinear
interpolation without antialiasing, then center-crop 224. Preserve float32 values;
do not quantize these tensors through uint8 before either runtime. Freeze
per-image hashes, input array, original model, reference outputs and manifest.
Reference execution: TensorFlow CPU 2.20.0 TFLite interpreter, one thread,
NumPy 2.2.6. It is a reference computation, not a CPU performance baseline.

## Gates and budget

All existing gates remain: top-1 label accuracy ≥75/100, unrestricted NPU/reference
top-1 agreement ≥90/100, absolute accuracy loss ≤0.05. A complete quality rejection
is terminal for this contract. Do not reinterpret the three-run follow-up as
300 independent accuracy samples or estimate full ImageNet accuracy from this
class-balanced subset.

One qualification Job: four warmups followed by 100 synchronous calls, one
Hailo-8, one CPU, at most 1 GiB host RAM, existing dedicated queue, 180-second
execution cap, no retries. Preserve HailoRT/driver/firmware 4.23.0, device-plugin
allocation and node protection. No driver, Kubernetes or quota upgrade.
Record downloaded bytes/durations, fixture/reference and image preparation,
queue/admission, physical reservation and call timing separately. Do not sum
overlapping preparations or infer NPU utilization/memory/power.

Only if all quality gates pass: implement a contract-bound execution adapter,
register the exact qualified model/input/runtime, and run three independent observation API
Jobs under the same limits. Require identity/result validation, one ledger row
and MLflow run per attempt, matching S3/API/MLflow result bytes, and a measured
lookup followed by immutable approval for one additional verification Job. At most
five allocated Jobs total (one qualification, three observations, one approved
verification). Any failed API attempt remains counted; no replacement to obtain
three successes.

## Reproduction and sources

The script [prepare_hailo_resnet50.py](../examples/prepare_hailo_resnet50.py) checks
archive and exclusion hashes before running the reference. Its output is accepted
by the qualification runner with a declared float32 input format; historical
manifests retain their uint8 default and unchanged verdicts.

- [Network/reference contract](https://github.com/hailo-ai/hailo_model_zoo/blob/v2.17/hailo_model_zoo/cfg/networks/resnet_v1_50.yaml)
- [Compiled normalization](https://github.com/hailo-ai/hailo_model_zoo/blob/v2.17/hailo_model_zoo/cfg/alls/generic/resnet_v1_50.alls)
- [Vendor preprocessing](https://github.com/hailo-ai/hailo_model_zoo/blob/v2.17/hailo_model_zoo/core/preprocessing/classification_preprocessing.py)
- [Original archive](https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/Classification/resnet_v1_50/pretrained/2025-01-15/resnet_v1_50.zip)
- [Compiled HEF](https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v2.17.0/hailo8/resnet_v1_50.hef)
