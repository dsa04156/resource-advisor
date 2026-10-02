"""Generate public deterministic input/checkpoint files; does not qualify hardware."""

import argparse
import json
from pathlib import Path

from resource_advisor.contracts import signature


def write_fixture(directory):
    checkpoint = {
        "weights": [[0.1], [-0.2]],
        "bias": [0.05],
        "momentum_weights": [[0.01], [0.02]],
        "momentum_bias": [0.005],
        "step": 0,
        "lr": 0.05,
        "momentum": 0.9,
    }
    x = [[float(i) / 8, float((i * 3) % 7) / 7] for i in range(8)]
    inputs = {"x": x, "y": [[2 * a - 3 * b + 0.5] for a, b in x]}
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {}
    for name, value in [("checkpoint", checkpoint), ("input", inputs)]:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        path = directory / (name + ".json")
        path.write_bytes(data)
        path.chmod(0o600)
        manifest[name] = {"digest": signature(value), "size_bytes": len(data)}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output_directory", type=Path)
    print(json.dumps(write_fixture(parser.parse_args().output_directory), indent=2))
