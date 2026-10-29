"""Enumerate launchable training allocations from qualified per-node evidence.

Builds candidates for a requested GPU count from real capability snapshots only:
count 1 is the existing single-device candidate on each qualified node; larger
counts are distinct physical nodes (or one node whose physical allocatable count
covers every rank). Nothing here measures, ranks or predicts JCT; a distributed
candidate still needs its own DDP qualification and profile before any estimate.
"""

from itertools import combinations

from .contracts import Candidate, CapabilitySnapshot, DistributedNode, DistributedTraining, now
from .policy import SHARED_IDENTITY, signature


def _usable(cap: CapabilitySnapshot, base: CapabilitySnapshot, validation_by_model, at):
    age = (at - cap.observed_at).total_seconds()
    return (
        cap.ready
        and cap.allocation_mode == "physical_device"
        and cap.reservation_verified
        and cap.allocatable_count >= 1
        and -5 <= age <= cap.valid_for_seconds
        and cap.accelerator_model in validation_by_model
        and all(getattr(cap, k) == getattr(base, k) for k in SHARED_IDENTITY)
    )


def build_candidates(
    base: Candidate,
    base_cap: CapabilitySnapshot,
    capabilities,
    validation_by_model: dict[str, str],
    *,
    counts=(1, 2, 4),
    collective="nccl",
    at=None,
    max_sets=16,
):
    """Candidates per requested GPU count; counts that no real node set covers are absent.

    ``validation_by_model`` maps accelerator model -> the TRAINING qualification ref of
    this exact variant on that model (it must also be in the variant's validation refs).
    Every set includes the base candidate's own node, whose capability anchors it.
    """
    at = at or now()
    nodes = sorted(
        (c for c in capabilities if _usable(c, base_cap, validation_by_model, at)),
        key=lambda c: c.node_ref,
    )
    if base_cap not in nodes or len({c.node_ref for c in nodes}) != len(nodes):
        return {}
    others = [c for c in nodes if c.ref != base_cap.ref]
    result = {}
    for count in sorted(set(counts)):
        if count == 1:
            result[1] = [base]
            continue
        made = []
        sets = [(base_cap, *rest) for rest in combinations(others, count - 1)]  # one GPU per node
        if base_cap.allocatable_count >= count:
            sets.append((base_cap,) * 1)  # same node, every rank on it
        for chosen in sets[:max_sets]:
            per_node = count // len(chosen)
            members = sorted(chosen, key=lambda c: c.node_ref)
            d = DistributedTraining(
                strategy="ddp",
                launcher="torchrun",
                collective=collective,
                nodes=tuple(
                    DistributedNode(
                        node_ref=c.node_ref,
                        capability_ref=c.ref,
                        gpus=per_node,
                        accelerator_model=c.accelerator_model,
                        validation_ref=validation_by_model[c.accelerator_model],
                    )
                    for c in members
                ),
                nproc_per_node=per_node,
                world_size=count,
            )
            context = base.context.model_copy(
                update={
                    "distributed": d,
                    "resources": base.context.resources.model_copy(
                        update={"accelerator_count": per_node}
                    ),
                }
            )
            ref = f"{base.ref}-ddp{count}-{signature(d.model_dump(mode='json'))[7:19]}"
            made.append(
                Candidate.model_validate(
                    base.model_copy(update={"ref": ref, "context": context}).model_dump()
                )
            )
        if made:
            result[count] = made
    return result
