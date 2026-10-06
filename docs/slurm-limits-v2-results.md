# Orin limits v2: GPU denial fixed, parent OOM state differs

The [frozen v2 plan](slurm-limits-v2-plan.md) ran three real Slurm allocations.
The corrected one-GPU device group permits the reserved CUDA kernel and denies
the same kernel without a GPU reservation. Memory enforcement also killed the
bounded overrun, but Slurm reported `FAILED` on the parent and `OUT_OF_MEMORY`
on its execution step. The planned **parent-state acceptance therefore failed**;
this report retains that result and does not rerun or relabel the trial.

| Probe | Kernel evidence | Scheduler result | Frozen acceptance |
|---|---|---|---|
| One GPU, 512 MiB | 4,096 correct CUDA outputs; memory cap 536870912, swap 0, CPU affinity 1 | COMPLETED | PASS |
| No GPU, 256 MiB | EPERM on `/dev/nvidia0`, `/dev/nvgpu/igpu0/ctrl`, `/dev/dri/renderD128`; `cuInit` error 801; cap 268435456, swap 0, CPU affinity 1 | COMPLETED (denial verifier) | PASS |
| No GPU, 64 MiB | Exact cap 67108864 checked before allocation; one cgroup OOM kill; swap 0, CPU affinity 1 | Parent FAILED / step OUT_OF_MEMORY | FAIL: parent state mismatch |

[Raw accounting and logs](evidence/slurm-limits-v2.json) retain all steps, source
hashes and costs. The three allocations record two total allocation seconds and
one GPU reservation second. Slurm's whole-second zero duration for the OOM step
does not mean zero execution or cost. Fast-step MaxRSS remains unknown.
Predecessor costs remain separate: v1 one GPU reservation second, the reserved
device trace two, and the earlier CNN qualification 21. The current driver-only
limits investigation totals **four GPU reservation seconds** across both trials
and the trace. It is not a performance benchmark or a GPU busy-time measurement.

## Correction and preservation

The [reserved trace](evidence/slurm-orin-device-trace.json) observed actual CUDA
opens through the Jetson GPU and DRM paths, with no `/dev/nvidia0` open. The
[reviewed 33-device manifest](evidence/slurm-orin-device-group.json) was checked
against every character-device identity. Installed Slurm 24.11.5 validated the
staged `MultipleFiles` configuration as `Count=1 Index=0`. The change retained
the exact previous GRES file, drained only the allocation-free selected worker,
replaced its GRES mapping, restarted its daemon and released only its own drain.
Both nodes returned IDLE and the queue is empty. The GPU count remains one.

Before/after comparisons preserve controller and other-worker configuration and
daemon state, QOS/associations, worker memory settings, driver identities and host
boot identity. The separately managed limits Ansible play then completed with
`changed=0`; all file hashes and daemon PIDs stayed unchanged through the trial
and repeat. **The GRES correction itself is a guarded manual operation, not yet
an Ansible-managed deployment.** Multi-file Orin use remains an explicitly tested
lab extension, not vendor-certified support, MIG or direct-SSH containment.

## Preserve the actual failure cause

The adapter previously returned generic `FAILED` for this real memory failure.
It now keeps the parent terminal state and allocation timing, while reporting
`OUT_OF_MEMORY` as the cause when an account-owned child step, inside that exact
allocation's time interval, proves it. It does not reinterpret successful or
canceled parent Jobs, borrow another Job's steps, or invent a GPU allocation.

Local adapter code queried the **same actual failed allocation**, without a new
submission, and returned `FAILED` with cause `OUT_OF_MEMORY`, 64 MiB allocation
and unchanged timestamps. Regression tests cover foreign IDs/accounts/partitions,
outside or missing time bounds, successful/canceled parents and the real record
shape. Test doubles are software evidence; the retained live readback is separate.
This adapter change has not yet been deployed to the platform worker. No API Job,
MLflow run or automatic PyTorch runtime registration was created by this trial.

The native PyTorch architecture warning, full Slurm API/model/artifact execution,
project authorization and controller/configuration automation remain open. The
Pi's absent accelerator and missing memory controller remain separate limitations.
