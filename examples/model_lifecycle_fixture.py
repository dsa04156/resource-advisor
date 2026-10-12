"""Freeze a public disjoint holdout and gate before starting qualified GPU training."""

import argparse
import json
from pathlib import Path

from resource_advisor.contracts import WorkloadSpec, signature
from resource_advisor.model_lifecycle import LifecyclePolicy


def make_policy(workload, training_input, *, ref, model_name, maximum_mse=1.0):
    # Midpoints differ from the fixture's i/8 training features. Targets follow
    # the same public generating function; these are synthetic regression data.
    x = [[(i + 0.5) / 8, (j + 0.5) / 8] for i in range(8) for j in range(8)]
    return LifecyclePolicy(
        ref=ref,
        project_ref=workload.project_ref,
        workload_ref=workload.ref,
        candidate_ref=workload.baseline_candidate_ref,
        workload_digest=signature(workload),
        model_name=model_name,
        training_input=training_input,
        holdout={"x": x, "y": [[2 * a - 3 * b + 0.5] for a, b in x]},
        maximum_mse=maximum_mse,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--training-input", type=Path, required=True)
    parser.add_argument("--policy-ref", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--maximum-mse", type=float, default=1.0)
    args = parser.parse_args()
    print(
        make_policy(
            WorkloadSpec.model_validate_json(args.workload.read_text()),
            json.loads(args.training_input.read_text()),
            ref=args.policy_ref,
            model_name=args.model_name,
            maximum_mse=args.maximum_mse,
        ).model_dump_json(indent=2)
    )
