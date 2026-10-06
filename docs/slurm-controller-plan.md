# Slurm controller inventory acceptance

Qualify the existing read-only collector against the actual, unchanged Slurm
24.11.5 controller before enabling continuous controller observations. No model
runtime capability is granted by this acceptance.

The first idle read found two source/fixture differences. The pinned
[v0.0.42 parser](https://github.com/SchedMD/slurm/blob/slurm-24-11-5-1/src/plugins/data_parser/v0.0.42/parsers.c)
returns success with `Zero jobs to dump` for an empty job message; this exact
warning is accepted only for an empty jobs list with no errors and the exact
qualified release. Other warnings, nonempty jobs and unsupported versions remain
unavailable. In the same release,
[`gres_get_node_drain`](https://github.com/SchedMD/slurm/blob/slurm-24-11-5-1/src/interfaces/gres.c)
always returns `N/A`: per-GRES drain reporting is unimplemented. Node-level
DRAIN/DOWN flags remain scheduling blockers. Explicit `gres_used` counts supply
missing zero TRES entries; absent, malformed or ambiguous counts stay unknown.
Reservation headroom is not physical utilization or admission approval.

## Fixed live trial

1. Read both allowed nodes and the configured account/partition queue through
   the local collector code. Require actual idle CPU/memory allocations, one
   configured Orin GPU, explicit zero GPU allocation and an observed empty queue.
2. With the queue still empty and the GPU worker IDLE, submit exactly two named
   lab Jobs under the unchanged account/QOS: a holder and a waiter. Each requests
   one typed GPU, one CPU, 512 MiB and two minutes. The holder executes the already
   qualified, SHA-pinned 4,096-element CUDA probe and then sleeps 30 seconds while
   retaining its allocation. The waiter has the same kernel and no added sleep.
3. During the holder, require one running and one pending record, one allocated
   GPU and zero GPU reservation headroom. Retain the scheduler's actual pending
   reason; do not presume which unchanged account/resource constraint caused it.
4. Cancel only the owned waiter while pending, using ID/account/partition/name
   filters. Observe the holder's terminal result and the waiter's cancellation,
   retain all accounting/logs and read a fresh idle inventory.

Maximum reservation is 240 GPU-seconds if both Jobs unexpectedly run to their
limits. Expected waiter reservation is zero, but use actual accounting instead of
assuming it. A queue observation timeout is not authority to resubmit; inspect
the same two IDs. Stop on unexpected acceptance and preserve costs. No host load
injection, driver/scheduler changes or other workload cancellation is part of this
trial. It tests controller observations, not CUDA utilization or model speed.

Continuous deployment additionally needs a dedicated read-only transport identity,
no submit/cancel authority in the inventory Pod, authenticated live API reads,
project isolation, freshness checks and an actual console readback. Do not report
those gates complete from local collector output alone.
