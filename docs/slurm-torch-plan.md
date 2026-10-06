# Orin native PyTorch qualification plan — 2026-10-06

This is a new, bounded F0 qualification after [scheduler recovery](slurm-recovery.md).
The existing Jetson Linux 39.2.1 and CUDA driver API 13.2 are preserved. An isolated,
operator-owned virtual environment uses the official PyTorch ARM64
`torch==2.12.1+cu132` wheel and `numpy==2.3.3`; installation reports retain every
resolved package version, source and archive hash. No system Python or driver
package is upgraded. A matching wheel tag alone does not establish GPU support.

Before execution, publish the [qualification source](../examples/qualify_slurm_torch.py).
Transfer that exact source and verify its SHA-256 on the worker. Submit exactly
one Slurm Job requesting one typed Orin GPU, one CPU, 1,024 MiB host memory and a
two-minute time limit under the existing normal lab QOS. Keep the scheduler ID,
source digest, stdout/stderr and terminal accounting even if it fails. An
observation timeout requires inspecting the same allocation, not resubmitting it.

The fixed fixture has seed 20261006, FP32 input `[4,3,32,32]`, two convolution
layers (8/16 channels), pooling and a ten-output linear layer. It uses generated
weights, three warmups and ten synchronized GPU forwards. All 400 output elements
must agree with the CPU reference at `rtol=atol=1e-4`. CUDA inputs, parameters and
outputs, exact PyTorch/CUDA versions and Orin capability 8.7 are required. No CPU
fallback is allowed. Deterministic algorithms and TF32 disabling are fixed before
the run; `CUBLAS_WORKSPACE_CONFIG=:4096:8` is supplied before importing PyTorch.

This establishes a bounded CNN execution environment if successful. It does not
establish trained-model accuracy, GPU isolation, cross-user authorization, a
statistical performance improvement or the full API→Slurm→result path. Forward
samples exclude CPU reference/transfer/startup. Report tensor allocation separately
from host RSS and scheduler GPU reservation; integrated GPU memory is not a
separate physical VRAM measurement. Unavailable power/utilization remain unknown.

Sources: [official CUDA 13.2 PyTorch wheels](https://download.pytorch.org/whl/cu132/torch/)
and [NVIDIA Orin CUDA setup](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/setup_cuda.html).
