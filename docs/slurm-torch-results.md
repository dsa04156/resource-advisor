# Orin PyTorch F0: numerical pass, execution gate blocked

October 6 follow-up: a separate
[isolated Jetson runtime](slurm-jetson-runtime-results.md) now passes the fixed
CNN with native `sm_87`, no runtime warnings and enforced 1 GiB memory. The first
candidate's failed gates and costs below remain unchanged; neither trial alone
establishes the full Slurm API/model delivery path.

This is the retained first CNN qualification. Subsequent
[cgroup/device enforcement](slurm-limits-v2-results.md) now limits the worker;
the unset TaskPlugin observation below describes the original run. The framework
compatibility finding and lack of automatic runtime registration still apply.

The [fixed plan](slurm-torch-plan.md) executed exactly one real Slurm GPU Job on
October 6. It completed with exit `0:0`, and all 400 compared CNN outputs agreed
with the CPU reference. The runtime is **not registered for automatic execution**:
PyTorch emitted an explicit Orin support warning, and measured process peak RSS
exceeded the requested host memory. Both findings remain in the
[raw evidence](evidence/slurm-orin-torch-f0.json).

| Boundary | Observed |
|---|---|
| Framework / CUDA userspace | PyTorch 2.12.1+cu132 / CUDA 13.2 |
| GPU | Orin, capability 8.7; CUDA input, parameters and outputs verified |
| Work | Three warmups, ten GPU CNN forwards, fixed generated input/weights |
| Numerical agreement | 400/400 elements, maximum absolute error 5.96e-8 |
| Median synchronized forward | 1.538 ms, one Job; not a cross-Job estimate |
| Torch peak tensor allocation | 32.305 MiB; not total unified-memory use |
| Process peak RSS | 1,083.230 MiB, above the requested 1,024 MiB |
| Scheduler allocation | One GPU, one CPU, 1,024 MiB, two-minute limit |
| Actual reservation | 21 seconds, hence 21 GPU reservation seconds |
| API Jobs / new supported variants | Zero / zero |

The official ARM64 wheel advertises `sm_80`, `sm_90`, `sm_100`, `sm_110`, `sm_120`.
Its warning explicitly excludes capability 8.7 from its stated support. The
observed CNN success remains real evidence for those operations; it does not
erase the warning or prove arbitrary Orin kernels are supported. Keep the warning
visible and resolve compatibility before promoting this environment.

The scheduler still has `TaskPlugin=(null)`. A memory request and successful job
completion therefore do not prove memory-limit enforcement. RSS is also only the
measured Python process maximum, not the entire job cgroup. Do not use 32 MiB of
Torch tensor allocations as the host-memory requirement for this unified-memory
device. A future attempt needs an independently declared runtime/resource plan
and appropriate lab admission/enforcement; it must retain this attempt's cost.

Installation used a separate root-owned virtual environment, leaving system
Python and the existing Jetson driver intact. The
[30-package environment lock](evidence/slurm-orin-torch-environment.json) includes
exact versions, download URLs and archive hashes. The source hash matched the
published pre-run commit on the worker before execution. Neither installation
success nor the script's numerical `PASS` grants an API execution capability.

Both Slurm nodes returned to `IDLE` after this attempt. No retry was submitted.
Pi DEEPX detection, fully qualified model/API/results, scoped worker identities,
cross-user authorization, enforcement and failure recovery remain open.
