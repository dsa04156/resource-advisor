# Orin Jetson runtime: strict GPU CNN qualification passed

The [published follow-up plan](slurm-jetson-runtime-results.md) ran exactly one actual
Slurm GPU Job on October 6. It completed with exit `0:0` under enforced one-CPU,
1,024 MiB memory and zero-swap boundaries. All 400 CNN output comparisons passed.
The NVIDIA Jetson wheel contains native `sm_87` code; runtime UserWarnings were
treated as errors and none occurred. This resolves the earlier warning/memory
gates **for this fixed generated CNN and isolated environment**.

| Observation | Actual result |
|---|---|
| Python | Isolated 3.10.20 ARM64; system Python retained |
| PyTorch / userspace CUDA | `2.5.0a0+872d972e41.nv24.08` / 12.6 |
| Device | Orin, compute capability 8.7 |
| Native GPU code | `sm_87` and `compute_87` reported; ELF inspection also finds `sm_87` |
| Numerical agreement | 400/400 elements; maximum absolute error 1.49e-8 |
| Process peak RSS | 788.590 MiB, below the 1,024 MiB request |
| Limiting cgroup memory peak | 646.520 MiB |
| PyTorch peak tensor allocation | 33.056 MiB |
| Median synchronized forward | 1.884 ms, ten samples within one Job |
| Scheduler interval | 03:47:16–03:47:25 UTC; 9 GPU reservation seconds |
| New API Jobs / automatically registered variants | 0 / 0 |

The input and weight hashes match the earlier fixed fixture. This is numerical
correctness of generated data and weights, not trained-model accuracy. Different
framework/library versions and one Job per candidate do not establish a speedup.
The previous candidate's warning and memory overrun remain failed gates in its
[unchanged evidence](../evidence/slurm-orin-torch-f0.json), including its 21 GPU
reservation seconds. The two CNN candidate trials therefore cost 30 GPU reservation
seconds in total; administrative download/install work is not included in GPU time.

Process RSS, cgroup charges and Torch tensors have different accounting scopes.
Shared mapped pages may be resident in a process but charged to another cgroup;
do not substitute one peak for another or call Torch allocation physical VRAM on
an integrated GPU. `sacct` supplied no model-step MaxRSS and recorded zero model
TotalCPU. Retain those raw observations without substituting batch-shell CPU/RSS
or claiming that the model consumed no CPU. GPU power/utilization were not measured.

## Reproducible inputs and boundaries

The [Python/OpenBLAS](../evidence/slurm-jetson-python-manifest.json) and
[CUDA userspace](../evidence/slurm-jetson-userspace-manifest.json) manifests pin the
actual interpreter, ten Python packages, OpenBLAS and thirteen NVIDIA archives.
All NVIDIA userspace archives passed their published size/SHA-256 checks before
extraction. OpenBLAS was extracted using existing signed apt metadata, not
installed globally. The new runtime is root-owned; system Python, driver, linker
cache and earlier runtime are unchanged. `LD_LIBRARY_PATH` applies only to this
candidate's preflight and fixed Slurm job.

A GPU-hidden import preflight found no missing dynamic libraries and reported
the expected framework/CUDA/native architectures without warnings. The installed
qualification source hash matched pre-run commit
`1e6653fa2e23aa8014b0222a40f7f12ac4a62273`. The GPU trial checked the actual inherited
limits before importing Torch and required CUDA inputs, parameters and outputs.
No CPU fallback or repeat submission occurred. Both nodes returned to IDLE with
an empty queue. Compared with the preceding verified snapshot, controller/worker
configuration hashes, boot and driver identities, daemon state, QOS and account
associations were unchanged.

[Raw sanitized qualification evidence](../evidence/slurm-orin-torch-v2.json) includes
both boundary observations, all ten timings, full stdout and parent/step records.
Preparation retained a blocked interpreter mirror/checksum request; official
GitHub release metadata and its asset digest provided a verified alternative.
An initial local remote-script templating error failed to parse before any remote
mutation; the corrected downloader ran once. The initial loader scan reported
missing dependencies; the pinned isolated userspace supplied them. None of these
setup checks submitted a GPU Job.

The [implementation CI](https://github.com/dsa04156/resource-advisor/actions/runs/37409827188)
passed on Python 3.11/3.13: 903 tests with two DB tests skipped without PostgreSQL,
then all 905 with PostgreSQL. Local tests passed 903 with two DB tests skipped.
Tests cover inherited limits, unavailable peaks, rejected non-Slurm execution,
driver/archive-source rejection, corrupt downloads and non-overwriting extraction.

## What remains open

This old Jetson wheel on the current host remains a **locally verified experimental
combination**, not NVIDIA certification of that JetPack pairing. The qualification
applies to the fixed CNN operations; it does not promise arbitrary framework
operators, training, compiled extensions or models.

Next qualify the actual platform workload contract and complete its pinned native
manifest, scoped submit/result credentials, API submission, artifact/MLflow and
usage delivery, cancellation and multi-user/failure checks. Pi NPU detection and
execution remain unresolved. No placeholder accelerator or automatic supported
runtime was registered by this trial.
