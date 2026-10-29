# Distributed (multi-GPU) training allocation

Status: contract, admission and Kubernetes launcher implemented with focused tests against
kubectl doubles. **No native multi-GPU run and no collective qualification exist yet.** No
distributed candidate is "ready" until a real two-rank run on the exact nodes passes. Its
JCT is unknown until measured; it is never derived by 1/N scaling of a single-GPU profile.

## Contract (`contracts.py`)

`ExecutionContext.distributed: DistributedTraining | None` (omitted when `None`, so
existing context and workload signatures are byte-identical; pinned by
`tests/fixtures/live-single-gpu-context-20261029.json`, a live ra-lab job).

| field | meaning |
|---|---|
| `strategy="ddp"`, `launcher="torchrun"` | data parallel; every rank holds a full model replica |
| `collective` | `nccl` (NVIDIA GPUs only) or `gloo` |
| `rendezvous="c10d-index0"` | torchrun c10d rendezvous hosted by completion index 0 |
| `nodes` | node SET, sorted by `node_ref`, distinct, each with its own `capability_ref` and `gpus` |
| `nproc_per_node` | ranks (= GPUs) per node; every node equal |
| `world_size` | `len(nodes) × nproc_per_node`, ≥ 2 |
| `memory_semantics="replicated-per-rank"` | per-rank VRAM requirement, never divided |

`placement` is `same-node` (one node, several GPUs) or `multi-node`.
`context.resources` is **per node/Pod**: `accelerator_count == nproc_per_node`; total GPUs =
`world_size`. Ranks are not preassigned to nodes; the scheduler places them and the
collector verifies the observed set.

`RuntimeVariant.distributed_launch="torchrun-ddp"` (omitted when `None`) is required for
a distributed candidate and part of its context signature.

## Admission (`policy.py`, single source)

`compatibility(..., node_caps=…)` / `execution_compatibility(..., node_caps=…)` add these
reasons. `Service.submit` and the worker's pre-submission check resolve `node_caps` with
`Service.node_capabilities`. A caller that passes no per-node evidence gets
`DISTRIBUTED_NODE_EVIDENCE_REQUIRED` (fail closed).

- `DISTRIBUTED_LAUNCH_MISMATCH`: the variant does not declare `torchrun-ddp`, or a
  non-distributed context uses a distributed variant.
- `DISTRIBUTED_TRAINING_ONLY`.
- `DISTRIBUTED_GLOBAL_BATCH_MISMATCH`: requires `global_batch_size == batch_size (per-rank
  micro) × world_size × gradient_accumulation`. The global batch is fixed; the micro batch
  changes with world size.
- `DISTRIBUTED_SHARED_SLOT_UNSUPPORTED`: virtual slots (e.g. `nvidia.com/gpu.shared`) are not
  GPUs. A shared count of 2 on one Orin is one device.
- `DISTRIBUTED_COLLECTIVE_UNSUPPORTED`: non-GPU devices, or `nccl` on a non-NVIDIA device.
- `DISTRIBUTED_SLURM_UNSUPPORTED`.
- `DISTRIBUTED_CANDIDATE_NODE_OUTSIDE_ALLOCATION`.
- Per node, against its own capability:
  - `…_EVIDENCE_MISSING`;
  - `…_CAPABILITY_MISMATCH` (node name smuggled under another capability);
  - `…_HETEROGENEOUS` (arch, vendor, model, allocation mode, resource key, runtime,
    environment, memory or power differ from the candidate's capability);
  - `…_STALE` (never relaxed, even in operational observe mode);
  - `…_UNAVAILABLE`;
  - `…_INSUFFICIENT_GPUS` (physical `allocatable_count`, verified reservation);
  - `…_INSUFFICIENT_HOST`.

## Kubernetes launcher (`backends.py`)

- **One Kueue-gated Job:**
  - `suspend: true` with the queue label (Kueue admits the whole PodSet);
  - `completionMode: Indexed`, `completions = parallelism = len(nodes)`, `backoffLimit: 0`
    (one rank failure fails the Job and terminates the other Pods);
  - the GPU request is **per Pod** (`nproc_per_node`).
- **Placement:**
  - required node affinity `kubernetes.io/hostname In <node set>`;
  - for multi-node, required pod anti-affinity on the attempt label (one Pod per host);
  - no `nodeName` and no hostname nodeSelector.
- **Unsupported, fail closed:** pool targets, termination retention, host network, and a
  configured single-hostname allowlist that conflicts with the node set.
- **Rendezvous:**
  - `subdomain = attempt`. The Pod DNS name is `<attempt>-<index>.<attempt>`
    (Kubernetes Indexed Job pod-to-pod pattern).
  - The headless Service `<attempt>` is created only after the Job UID is known. It has
    `ownerReferences` to that UID (garbage-collected with the Job), `publishNotReadyAddresses: true`
    (rendezvous precedes readiness), and a selector on the attempt label plus
    `batch.kubernetes.io/controller-uid`.
  - Reconcile recreates a lost Service and refuses a foreign one (`DISTRIBUTED_SERVICE_CONFLICT`).
    It never deletes a Service.
- **Command:**
  - `python -m torch.distributed.run --nnodes=N --nproc-per-node=P --rdzv-backend=c10d
    --rdzv-id=<attempt> --rdzv-endpoint=<attempt>-0.<attempt>:29400 --max-restarts=0`,
    followed by the qualified script or `-m module` and its args.
  - Env adds `RA_DISTRIBUTED_JSON` and `RA_NODE_INDEX` (completion-index annotation).
  - torchrun itself sets `RANK/LOCAL_RANK/WORLD_SIZE/LOCAL_WORLD_SIZE/MASTER_ADDR/MASTER_PORT`.
  - Flags follow the torchrun docs (c10d form). `-m` pass-through relies on `torch.distributed.run`'s
    module option and should be confirmed by the runtime qualification.
- **Status (all ranks of this Job UID):**
  - A rank off the node set, or two ranks on one host, gives `FAILED
    DISTRIBUTED_PLACEMENT_MISMATCH` and the Job is deleted.
  - `Complete` counts as success only when every index exists, every Pod Succeeded, and the
    observed hosts equal the node set; otherwise `DISTRIBUTED_PLACEMENT_UNVERIFIED`.
  - Placement records every rank (index, Pod UID, node). Allocation sums per-Pod requests.
- **Result:** read from every rank Pod log. Exactly one envelope is required (rank 0 emits
  it); zero or several is rejected.
- **Cancel:** deletes only the owned Job; the Service goes with it by GC.

## Slurm

Explicit limitation: `DISTRIBUTED_SLURM_UNSUPPORTED` in admission and the Slurm adapter. No
verified multi-node `--nodes/--ntasks/--gpus-per-node` + torchrun shape exists on the lab
cluster yet, and none is guessed.

## Runtime obligations (vision training owner)

- Initialize from torchrun env.
- Use `DistributedSampler` (or equivalent) with the exact global batch.
- Only `RANK == 0` emits `RESOURCE_ADVISOR_RESULT` and the receipt.
- Wall time includes communication.
