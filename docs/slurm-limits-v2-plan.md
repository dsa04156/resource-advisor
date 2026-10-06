# Orin device-group correction and limits v2

The [stopped v1 trial](slurm-limits-results.md) and its reserved `openat` diagnostic
establish that `/dev/nvidia0` is not the CUDA execution boundary on this Jetson.
The diagnostic completed one reserved GPU Job with correct 4,096-element output
and cost two GPU reservation seconds. It opened `/dev/nvgpu/igpu0/ctrl`, its power
interface and `/dev/dri/renderD128`; `/dev/nvidia0` was not opened.

## Reviewed mapping

The [fixed device manifest](evidence/slurm-orin-device-group.json) groups 33 unique
character-device identities for the one integrated GPU: its current `nvgpu/igpu0`
interfaces, legacy `nvhost*gpu` interfaces, the DRM nodes sharing the observed
render node's sysfs device, and the existing compatibility `/dev/nvidia0`.
Shared `/dev/nvmap` and `/dev/host1x-fence` are not assigned as dedicated GPU files.
Require every listed path's character-device major/minor to match before applying.
This is a specific lab mapping, not automatic discovery for arbitrary Jetsons.

Use `MultipleFiles` with `Count=1`, preserving `Type=orin_nano` and the existing
one-GPU scheduler count. The installed Slurm 24.11.5
[parser/device mapping implementation](https://github.com/SchedMD/slurm/blob/slurm-24-11-5-1/src/interfaces/gres.c)
sets one resource count and one allocation bitmap index for all member files.
Its [manual](https://github.com/SchedMD/slurm/blob/slurm-24-11-5-1/doc/man/man5/gres.conf.5)
describes the usual MIG case and recommends `File` otherwise. Applying the generic
multi-file mechanism to this non-MIG Jetson is therefore an explicitly tested lab
extension, **not a claim of MIG or vendor-certified Orin support**. The positive
and negative tests below must prove its behavior before claiming the bounded fix.

Retain the exact previous GRES file. With the queue empty, drain only the selected
idle worker, verify zero allocations, validate a staged configuration using the
installed `slurmd -G`, and replace only that worker's GRES file. Restart only its
drained daemon, then release only our own drain after registration. Preserve the
controller, Pi, QOS/associations, worker memory limits, drivers and boot identity.
Record an exact-hash mapping receipt. Configuration automation for this separate
mapping correction is not yet included in the existing limits Ansible role.

## New fixed trial

Use a separate immutable v2 source directory and new attempt names. Run the same
three bounded cases in order: reserved CUDA kernel (one GPU/512 MiB), no-GPU
denial (256 MiB), and contained memory overrun (64 MiB, capped 160 MiB request).
All request one CPU and at most two minutes; maximum additional reservation is
120 GPU-seconds and six allocation-minutes. Require inherited exact memory bounds,
zero swap and one CPU affinity before each probe.

The v2 negative must receive EPERM for all three explicitly tested interfaces:
`/dev/nvidia0`, `/dev/nvgpu/igpu0/ctrl`, `/dev/dri/renderD128`. It must also fail the
same real CUDA initialization/device/context path, rather than merely hide an
environment variable. The positive must compute all 4,096 values correctly.
The memory negative must end as `OUT_OF_MEMORY`. Stop on an unexpected outcome,
preserve all evidence and inspect the same allocation after an observation timeout.

V1 remains a failure; do not relabel it. Its two Jobs cost one GPU reservation
second and four allocation seconds and include observed unreserved GPU work.
The separate trace cost another two GPU reservation seconds. The earlier CNN F0
cost 21 GPU reservation seconds and remains a distinct runtime qualification.
No improvement, full multi-user isolation, direct-SSH containment or automatic
PyTorch runtime promotion is claimed by this trial.
