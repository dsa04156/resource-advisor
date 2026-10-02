"""Render a bounded Hailo reservation check; never submits or mutates a cluster."""

import argparse
import json
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["control", "unallocated", "oversize"], required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", args.image):
        parser.error("an immutable image digest is required")
    here = Path(__file__).parent
    job = json.loads((here / "job.json").read_text())
    job["metadata"].update(name=args.name, namespace=args.namespace)
    job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] = args.queue
    job["spec"]["activeDeadlineSeconds"] = 60
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["image"] = args.image
    if args.case == "oversize":
        container["command"] = [
            "python",
            "-c",
            'raise SystemExit("UNEXPECTED_OVERSIZE_ADMISSION")',
        ]
    else:
        container["command"] = [
            "python",
            "-c",
            (here / "device_access.py").read_text(),
            "--expect",
            "allowed" if args.case == "control" else "denied",
        ]
    for kind in ("requests", "limits"):
        if args.case == "unallocated":
            container["resources"][kind].pop("hailo.ai/h8")
        else:
            container["resources"][kind]["hailo.ai/h8"] = "2" if args.case == "oversize" else "1"
    print(json.dumps(job, indent=2))


if __name__ == "__main__":
    main()
