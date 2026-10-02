"""Run a synthetic ask/observe loop. No GPU, scheduler or hardware evidence.

This fixture has explicit fidelity-dependent bias and per-evaluation cost.
It tests numerical behavior, not whether any real workload has this relation.
"""

import argparse
import json
import math
from pathlib import Path

from resource_advisor.contracts import signature
from resource_advisor.mfkg import MFKernelInput, MFObservation, MFOption, ask_mfkg

GROUP = signature("synthetic homogeneous MF numerical fixture")


def fixture():
    options = tuple(
        MFOption(ref=f"x{i}-s{j}", candidate_ref=f"x{i}", coordinates=(x,), fidelity=s)
        for i, x in enumerate((0.0, 0.5, 1.0))
        for j, s in enumerate((0.25, 1.0))
    )
    observations = tuple(observe(o, repeat) for o in options for repeat in range(3))
    return MFKernelInput(
        runtime_group_signature=GROUP,
        fidelity_axis="representative_sampling",
        feature_names=("normalized_numeric_setting",),
        options=options,
        observations=observations,
        seed=17,
        num_fantasies=16,
    )


def observe(option, repeat):
    x, s = option.coordinates[0], option.fidelity
    # Explicit mathematical fixture, not an observation from a device.
    target = 10.0 + 0.1 * (x - 0.65) ** 2
    bias = (1 - s) * (0.3 + 0.2 * x)
    noise = 0.12 * math.sin(repeat * 2.1 + x)
    return MFObservation(
        attempt_id=f"synthetic-{option.ref}-{repeat}",
        option_ref=option.ref,
        runtime_group_signature=GROUP,
        seconds_per_work_unit=target + bias + noise,
        evaluation_wall_seconds=2 + 8 * s,
        quality_passed=True,
        memory_passed=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new report path")
    problem = fixture()
    trace = []
    for _ in range(3):
        choice = ask_mfkg(problem)
        step = {"input": problem.model_dump(mode="json"), "choice": choice, "observation": None}
        trace.append(step)
        if choice["option_ref"] is None:
            break
        option = next(o for o in problem.options if o.ref == choice["option_ref"])
        repeat = sum(r.option_ref == option.ref for r in problem.observations)
        observation = observe(option, repeat)
        step["observation"] = observation.model_dump(mode="json")
        problem = MFKernelInput.model_validate(
            {
                **problem.model_dump(),
                "observations": (*problem.observations, observation),
            }
        )
    report = {
        "evidence_kind": "synthetic_numerical_fixture",
        "execution_authorized": False,
        "hardware_jobs_submitted": 0,
        "trace": trace,
    }
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "evidence_kind": report["evidence_kind"],
                "steps": len(trace),
                "hardware_jobs_submitted": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
