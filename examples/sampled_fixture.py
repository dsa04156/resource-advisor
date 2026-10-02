"""Create an explicit synthetic finite population, never private research data.

Values differ per file. GPU execution of these inputs is real hardware evidence;
the generated distribution is only a functional fixture, not an ML dataset.
"""

import argparse
import hashlib
import json
import random
from pathlib import Path

from resource_advisor.sampling import SamplingPolicy, TensorBatch


def build(project, ref, directory):
    directory.mkdir(parents=True, exist_ok=False)
    rng = random.Random(20261003)
    population = []
    for i in range(8):
        stratum = "negative" if i < 4 else "positive"
        sign = -1 if i < 4 else 1
        values = tuple(sign * rng.randint(1, 128) / 128 for _ in range(64 * 64))
        raw = (
            TensorBatch(shape=(64, 64), precision="fp32", values=values).model_dump_json().encode()
        )
        name = f"input-{i:02}"
        (directory / (name + ".json")).write_bytes(raw)
        population.append(
            {
                "ref": name,
                "stratum": stratum,
                "content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }
        )
    policy = SamplingPolicy(
        ref=ref,
        project_ref=project,
        dataset_version="gram-two-strata-fixture-v1",
        input_shape=(64, 64),
        precision="fp32",
        batch_size=1,
        seed=20261003,
        population=tuple(population),
    )
    (directory / "policy.json").write_text(json.dumps(policy.model_dump(mode="json"), indent=2))
    return policy


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.project, args.ref, args.output)
