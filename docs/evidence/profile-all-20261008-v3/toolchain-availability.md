# NPU toolchain availability for the frozen Digits MLP rerun

Observed 2026-10-08. Scope: existing local artifacts, currently configured registry images, and three bounded native SDK-only Jobs. No model inference, firmware/device reset, driver installation, or new compiler installation was performed by this ticket.

## Result

The existing Hailo/Mobilint/Rockchip execution paths have runtime + precompiled artifacts, but the required same-Digits compiler was not found in the inspected installations/images/assets. This is a missing-toolchain/artifact finding, **not a proof that the hardware cannot execute the MLP**. Do not combine the unrelated existing templates into a same-model GPU↔NPU ranking.

| Runtime | Existing evidence | Missing same-model prerequisite | Executable fallback workload group |
|---|---|---|---|
| Hailo8, two physical devices | Fresh SDK Job on second device: HailoRT4.23.0, hailo_platform present; hailo_sdk_client absent. Existing pinned image has ResNet50 HEF. | Dataflow Compiler matching Hailo8/HailoRT (official MZ v2.17 lists DFC3.33.0), exported frozen MLP graph, calibration tensors, qualified compiled HEF. | Same frozen ResNet50 HEF/input/quality contract across both Hailo devices. |
| Mobilint ARIES device | Fresh SDK Job: qbruntime1.4.0 present, qbcompiler and Quiler absent; existing Candy/Mosaic MXQ only. | qb Compiler for ARIES target, frozen MLP ONNX/weights/calibration, generated MXQ matching selected core mode/runtime, numeric quality proof. | Frozen Candy MXQ/input; one scheduler candidate means no placement optimization claim. |
| RK3399Pro | Archived runtime layer and wheel are rknn_toolkit_lite1.7.1/rknnlite only, including precompiled official ResNet18. Fresh SDK Job failed before module discovery because Python3.7 lacks importlib.metadata. | Full **legacy RKNN Toolkit** (rknn.api.RKNN), frozen MLP graph/calibration and compiled RKNN targeting rk3399pro. Toolkit2 is not the target SDK. | Official frozen ResNet18 RKNN/input through existing trusted host bridge; retain server/runtime mismatch limits. |

Current node allocatable resources have Hailo1 on each of two Pi hosts, Mobilint1, Rockchip1, and Intel NPU1. Scheduler allocation units do not count internal ARIES cores as separate physical devices. Intel direct OpenVINO compilation is handled by the separate MLP implementation ticket.

## Native SDK-only receipts

Artifacts: `.state/resource-advisor/profile-all-20261008-v3/toolchain-probes/`.

- `profile-all-sdk-hailo-20261008`, queue ra-hailo-second: succeeded, native Job3s, container timestamps quantized to0s (nonnegative duration bound0–1s).
- `profile-all-sdk-mobilint-20261008`, queue ra-mobilint: succeeded, native Job3s, container timestamps quantized to0s (bound0–1s).
- `profile-all-sdk-rockchip-20261008`, queue ra-rockchip-npu: failed with ModuleNotFoundError for importlib.metadata, native Job5s, container timestamps quantized to1s (bound0–2s). No NPU inference attempted. No retry Job was submitted.

Each used existing pinned qualified image, native extended-resource request1, backoffLimit0 and activeDeadlineSeconds60. Manifests, create responses, final Job/Pod JSON, timestamped raw logs and Kueue receipts are preserved. All three Kueue Workloads have Finished=True; stale QuotaReserved=True condition is not evidence of still-active quota. `summary.json` retains timestamps, UIDs, image IDs, failed attempt and uncertainty. Zero quantized point estimates must not be reported as zero physical profiling cost.

Local scan found no Hailo DFC, qb Compiler/Quiler, or full RKNN Toolkit installer/module in inspected workspace `.state`, local Python environments, `/opt`, and matching local registry repositories. Search scope and runtime packages are not proof of absence in arbitrary uninspected hosts. `registry-runtime-repositories.json` records catalog scope; `rockchip-sdk-layer-members.json` and `rockchip-lite-wheel-metadata.txt` distinguish Lite runtime from compiler.

## Same-model conversion candidate

The fixed batch of256 independent64-feature vectors can be laid out as a16×16 feature map with64 input channels. Three1×1 convolutions64→256→256→10, with the original biases and intermediate ReLUs, implement the original three affine layers independently at each spatial location. Layout conversion plus reshaping output back to256×10 preserves the mathematical function in real arithmetic. This is a proposed equivalent graph, not a compiled or qualified NPU result. Quantization may alter logits; the original held-out top1/reference-logit quality contract must still pass before any performance comparison. Do not reuse an unrelated HEF/MXQ/RKNN as this model.

## Primary documentation checked

- [Hailo Model Zoo v2.17 installation and flow](https://github.com/hailo-ai/hailo_model_zoo/blob/1bde1102a7706e43688c863330a93acbc0f4db90/docs/GETTING_STARTED.rst): custom graphs use DFC directly; parse→optimization/calibration→HEF compilation is distinct from HailoRT inference. Retrieved with GitHub MCP; Context7 also confirms DFC role.
- [Mobilint qb Compiler overview](https://docs.mobilint.com/compiler/v1.3/en/introduction.html) and [compile flow](https://docs.mobilint.com/compiler/v1.3/en/basic_compile_flow.html): original model→MBLT→MXQ; ARIES target aries-rb. Runtime/core-mode compatibility is a separate requirement.
- [Legacy Rockchip SDK platform distinction](https://github.com/rockchip-linux/rknn-toolkit) and [pinned ONNX conversion example](https://github.com/rockchip-linux/rknn-toolkit/blob/a990fb76f5567d6783fba2fd951ccc8243bfc285/examples/onnx/resnet50v2/test.py): full rknn.api SDK supplies load_onnx→build→export_rknn. Lite deployment/runtime does not supply this conversion path.

No performance improvement is inferred from this exploration. The same-model request remains incomplete for Hailo/Mobilint/Rockchip until suitable compilers and qualified compiled frozen-MLP artifacts exist; separate native workload groups can satisfy actual execution coverage transparently.
