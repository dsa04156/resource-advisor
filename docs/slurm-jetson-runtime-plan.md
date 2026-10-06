# Isolated Jetson PyTorch candidate and strict F0 — October 6

The stock ARM64 PyTorch qualification retained an explicit unsupported-Orin warning
and excessive RSS. Preserve that failed execution gate and its 21 GPU reservation
seconds. This follow-up uses the NVIDIA Jetson wheel from the official
[v61 index](https://developer.download.nvidia.com/compute/redist/jp/v61/pytorch/):
`torch-2.5.0a0+872d972e41.nv24.08.17622132-cp310-cp310-linux_aarch64.whl`,
SHA-256 `6f75fd2d2ef840ede1a90dbcf40a5458214bee26cc803fa510cda2e8978d972a`.
Its embedded version is `2.5.0a0+872d972e41.nv24.08` and CUDA userspace is 12.6.

The [NVIDIA matrix](https://docs.nvidia.com/deeplearning/frameworks/install-pytorch-jetson-platform-release-notes/pytorch-jetson-rel.html)
does **not** certify this older wheel on the current Jetson Linux 39.2.1 host.
This is an experimental, independently qualified workload environment, not a
vendor-supported JetPack combination. No driver, boot, scheduler policy or system
Python replacement is permitted. A successful install/import is not qualification.

## Preparation gate

Use a new root-owned runtime directory, retaining the original candidate. The
Python 3.10.20 ARM64 standalone archive from Astral's 20260728 release is verified
against its official GitHub asset digest:
`50226919b4be3f13eee6a05b30b7241b43385deb7dc1673e7c5c67bf438124d4`.
Create an isolated venv; pin NumPy 1.26.4 and retain every resolved wheel URL,
version and archive hash. Inspect actual loader requirements before adding CUDA
12 userspace dependencies. Such dependencies must stay in this runtime directory;
do not install apt packages, change global loader configuration or copy libcuda
over the host driver. Publish the resulting environment manifest before GPU F0.

## Fixed GPU gate

Run the [CNN qualifier](../examples/qualify_slurm_torch.py) exactly once using
the published source digest, `--require-native-arch` and
`--enforced-memory-mib 1024`. Require explicit `sm_87` in the framework's compiled
architecture list. Runtime UserWarnings are errors, never suppressed. Require
the actual inherited cgroup memory limit to be 1,024 MiB, swap limit zero and CPU
affinity one before importing the framework. Record the limiting cgroup's peak
where available, separate from process RSS and PyTorch tensor allocation.

Use the unchanged normal lab QOS, one typed Orin GPU, one CPU, 1,024 MiB and a
two-minute limit. The fixture remains FP32 `[4,3,32,32]`, seed 20261006, three
warmups and ten synchronized CNN forwards; compare all 400 output elements to the
CPU reference at rtol/atol 1e-4. Keep input/weight hashes, all samples, full logs
and parent/step accounting, including OOM and setup failures. An observation
timeout never authorizes another submission.

No trained-model accuracy or performance improvement follows from this fixture.
Runtime registration and the complete API→Slurm→artifact/accounting path require
their own subsequent acceptance. Stop on failure, retain costs and diagnose before
publishing any different plan. Restore no configurations because none are changed;
verify protected worker/controller identities and an empty queue after the trial.

## Pinned preparation inputs

The [Python/OpenBLAS manifest](evidence/slurm-jetson-python-manifest.json) records
ten resolved Python packages, the standalone interpreter asset digest and
OpenBLAS `0.3.26+ds-1ubuntu0.1`. OpenBLAS is downloaded through the worker's existing
signed apt package metadata and extracted into the candidate directory; it is
not installed into dpkg. Its existing system libgfortran dependency is retained.

The [CUDA userspace manifest](evidence/slurm-jetson-userspace-manifest.json) selects
the explicit **linux-aarch64** archives rather than the distinct SBSA archives
from NVIDIA's [CUDA 12.6.3 redistributable manifest](https://developer.download.nvidia.com/compute/cuda/redist/redistrib_12.6.3.json),
[cuDNN 9.3.0 manifest](https://developer.download.nvidia.com/compute/cudnn/redist/redistrib_9.3.0.json)
and [cuSPARSELt 0.6.3 manifest](https://developer.download.nvidia.com/compute/cusparselt/redist/redistrib_0.6.3.json).
Each archive's published size and SHA-256 is checked before extraction by
[`prepare_cuda_userspace.py`](../examples/prepare_cuda_userspace.py). Drivers and
compatibility driver shims are excluded. Retain all archive/component versions;
these inputs do not claim GPU qualification.

Set `LD_LIBRARY_PATH` only in the fixed Slurm job to the extracted userspace and
OpenBLAS library directories. Check dynamic loading with that same environment
before submission. Do not run a Docker daemon workload outside the Slurm cgroup
or change the system-wide linker cache to make this candidate work.
