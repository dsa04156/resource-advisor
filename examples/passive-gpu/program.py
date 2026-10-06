"""An ordinary bounded CUDA program: no Resource Advisor imports or observer hooks."""

import argparse
import json
import time

import torch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=35)
    args = parser.parse_args()
    if not 0 < args.seconds <= 60:
        raise ValueError("bounded research-program duration required")
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("this fixture requires one actually visible CUDA device")
    torch.manual_seed(13)
    a = torch.randn(2048, 2048, device="cuda", dtype=torch.float32)
    b = torch.randn_like(a)
    result = a @ b
    torch.cuda.synchronize()
    properties = torch.cuda.get_device_properties(0)
    print(
        json.dumps({"event": "ready", "device": properties.name, "uuid": str(properties.uuid)}),
        flush=True,
    )
    deadline, iterations = time.monotonic() + args.seconds, 1
    while time.monotonic() < deadline:
        result = a @ b
        torch.cuda.synchronize()
        iterations += 1
        time.sleep(0.05)
    print(
        json.dumps(
            {"event": "finished", "iterations": iterations, "checksum": result.sum().item()}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
