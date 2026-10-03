# E5 kernel trace qualification: stopped v1

The [frozen v1 protocol](e5-kernel-plan.md) stopped after its two qualification
Jobs. No scheduled comparison Job was submitted, no worker binding was added,
and no API performance profile was created. The failed run remains in the cost.

| Qualification | Outcome | GPU reservation | Container time |
|---|---|---:|---:|
| cache, profiler off | Output agreement 1.0; eligible thermal trace | 3 s | 2 s |
| cache, profiler on | Trace parser raised `ValueError` | 3 s | 2 s |

The parser rejected a forward marker as duplicate or incomplete. It matches
marker names before checking their category; a GPU-correlated annotation with
the same name could therefore collide with a CPU range. This is a hypothesis,
not yet an observed root cause: temporary-file cleanup removed the original
trace after the exception. A local PyTorch 2.8 CPU-only trace contains the
expected `user_annotation` ranges but cannot establish CUDA behavior.

[Raw qualification evidence](evidence/e5-kernel-qualification-v1.json) retains
the complete successful result/phase/thermal envelope, failure, both costs and
preservation checks. Ten nodes remained Ready without pressure; existing DB
records, static specifications, Secrets, PVCs, queue quota and running Pods
were unchanged. The six GPU reservation seconds are allocation cost, not
kernel activity or billed use.

## One-shot trace capture probe, registered before execution

Use a **separate diagnostic probe**, never resume or replace v1's failed slot.
Maximum one GPU Job, 90 seconds execution/reservation upper bound, 90 seconds
queue wait, 300 seconds total observation; same physical GPU, CPU 1, 2 GiB host
memory, source ConfigMap, pinned runtime, model/input/seed/shape and cache arm.
No ordinary API result ingestion, workload registration or worker reload.

Only change the wrapper around the parser: print a bounded whitelist of the
exported trace's forward annotations and CUDA kernels **before** passing the
unchanged trace to the unchanged v1 parser. Retain names, categories, event
types, relative timestamps/durations and local CUDA device number; exclude
hostnames, process/thread IDs, paths and arbitrary trace metadata. Limit to
20,000 selected events and 2 MiB serialized evidence. Save the same Job UID,
its logs and allocation cost regardless of parser exit status. No retry.

The probe answers whether a same-name CPU/GPU annotation collision actually
occurs and provides a replayable parser regression fixture. Its elapsed times
must not enter the planned performance comparison. Apply any supported parser
fix to the saved trace offline first. A later comparison needs a separately
frozen protocol, must retain v1 and probe costs, and cannot change scientific
acceptance thresholds merely to obtain success.

## Probe outcome and offline correction

The one-shot probe reproduced the original exception. Its saved trace contains
12 CPU `user_annotation` ranges, 12 same-named `gpu_user_annotation` ranges and
480 CUDA kernel events. This directly confirms the category collision; the
[complete whitelisted input](evidence/e5-kernel-trace-probe-v1.json) is a public
regression fixture. The new regression failed before the fix and passed after it.

The parser now counts GPU-correlated annotations separately and excludes them
from both host ranges and kernel activity. Duplicate CPU ranges, missing actual
kernels, wrong devices and out-of-range kernels still reject the trace. The
runner prints bounded, whitelisted trace evidence before parsing, so a parser
exception can be replayed without another hardware run.

The probe exited 1 as expected from the unchanged old parser and consumed
3 GPU reservation seconds (2 container seconds). Combined v1 qualification and
probe cost is **9 GPU reservation seconds in three Jobs**. Offline replay with
the corrected parser succeeds, but does not convert the failed Job into a
successful benchmark or supply the six missing plain comparisons. A separately
registered [v2 protocol](e5-kernel-plan-v2.md) retains these failures and costs.
