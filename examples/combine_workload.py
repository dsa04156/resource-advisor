"""Combine qualified executions of the exact same logical workload into candidates.

Input files are build_cuda_target-style capability/variant/workload bundles.
This preserves existing qualification, images and contexts; it does not convert
models between accelerator vendors or create qualification evidence.
"""

import argparse
import json
from pathlib import Path

from resource_advisor.contracts import CapabilitySnapshot, RuntimeVariant, WorkloadSpec, signature


def combine(bundles, ref):
    specs = [WorkloadSpec.model_validate(b["workload"]) for b in bundles]
    if not specs or any(
        signature(s.identity) != signature(specs[0].identity)
        or signature(s.quality) != signature(specs[0].quality)
        or s.project_ref != specs[0].project_ref
        for s in specs
    ):
        raise ValueError(
            "Only the same project, logical workload and quality contract can be combined"
        )
    candidates, variants, capabilities = [], [], []
    for index, (bundle, spec) in enumerate(zip(bundles, specs, strict=True)):
        if len(spec.candidates) != 1:
            raise ValueError("Each source bundle must contain one qualified execution candidate")
        variant = RuntimeVariant.model_validate(bundle["variant"])
        cap = CapabilitySnapshot.model_validate(bundle["capability"])
        candidate = spec.candidates[0]
        if candidate.variant_ref != variant.ref or candidate.capability_ref != cap.ref:
            raise ValueError("Bundle references do not match")
        variant = variant.model_copy(
            update={"ref": f"{ref}-runtime-{index + 1}", "workload_ref": ref}
        )
        candidate = candidate.model_copy(
            update={"ref": f"candidate-{index + 1}", "variant_ref": variant.ref}
        )
        candidates.append(candidate)
        variants.append(variant.model_dump(mode="json"))
        capabilities.append(cap.model_dump(mode="json"))
    spec = specs[0].model_copy(
        update={
            "ref": ref,
            "candidates": tuple(candidates),
            "baseline_candidate_ref": candidates[0].ref,
        }
    )
    WorkloadSpec.model_validate(spec.model_dump(mode="json"))
    return {
        "workload": spec.model_dump(mode="json"),
        "variants": variants,
        "capabilities": capabilities,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("bundles", type=Path, nargs="+")
    args = parser.parse_args()
    result = combine([json.loads(p.read_text()) for p in args.bundles], args.ref)
    with args.output.open("x") as out:
        out.write(json.dumps(result, indent=2) + "\n")
