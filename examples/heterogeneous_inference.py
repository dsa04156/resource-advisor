"""Submit a prepared NHWC .npy image to a qualified pool; credentials stay in env."""

import argparse
import json
import os

import httpx


def main():
    import numpy as np

    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--deadline-ms", type=int, default=5000)
    parser.add_argument("--ca-file")
    args = parser.parse_args()
    data = np.load(args.inputs, allow_pickle=False)
    tensor = data if data.shape == (224, 224, 3) else data[args.index]
    if tensor.shape != (224, 224, 3):
        raise ValueError("prepared input must be RGB224 NHWC")
    headers = {"Authorization": "Bearer " + os.environ["RA_API_TOKEN"]}
    verify = True
    if args.ca_file:
        import ssl

        verify = ssl.create_default_context(cafile=args.ca_file)
    with httpx.Client(verify=verify, timeout=15) as client:
        response = client.post(
            args.api_url.rstrip("/") + "/api/v1/compute/inference-pools/" + args.pool + ":predict",
            headers=headers,
            json={
                "instances": [tensor.astype(float).reshape(-1).tolist()],
                "deadline_ms": args.deadline_ms,
            },
        )
        response.raise_for_status()
        result = response.json()
        print(
            json.dumps(
                {"model": result["model"], "top1": result["top1"], "routing": result["routing"]},
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
