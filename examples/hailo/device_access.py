"""Bounded device-open check; does not load a model or claim inference quality.

Run in otherwise identical non-root Pods with and without hailo.ai/h8 requests.
Use the allocated control first and verify both Pods ran on the same node.
"""

import argparse
import errno
import json
import os
from pathlib import Path


def probe(expect):
    import hailo_platform as hailo

    driver = Path("/sys/module/hailo_pci/version").read_text().strip()
    if driver != "4.23.0" or hailo.__version__ != "4.23.0":
        raise RuntimeError("device access probe requires the qualified 4.23.0 runtime")
    nodes = sorted({"/dev/hailo0", *(str(p) for p in Path("/dev").glob("hailo[0-9]*"))})
    opened, errors = [], []
    for node in nodes:
        try:
            fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        except OSError as exc:
            errors.append({"device": node, "errno": exc.errno})
        else:
            os.close(fd)
            opened.append(node)
    report = {
        "kind": "hailo-device-access",
        "expect": expect,
        "hailort": hailo.__version__,
        "driver": driver,
        "effective_uid": os.geteuid(),
        "opened": opened,
        "errors": errors,
        "architecture": None,
        "model_inference_performed": False,
    }
    if expect == "allowed":
        ids = hailo.Device.scan()
        if len(ids) != 1:
            raise RuntimeError("allocated control requires exactly one device")
        with hailo.Device(ids[0]) as device:
            report["architecture"] = str(device.control.identify().device_architecture)
        report["passed"] = len(opened) == 1 and report["architecture"] == "HAILO8"
    else:
        report["passed"] = not opened and all(
            error["errno"] in {errno.ENOENT, errno.EACCES, errno.EPERM} for error in errors
        )
    report["passed"] = report["passed"] and report["effective_uid"] != 0
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", choices=["allowed", "denied"], required=True)
    args = parser.parse_args()
    result = probe(args.expect)
    print("RESOURCE_ADVISOR_DEVICE_ACCESS " + json.dumps(result), flush=True)
    raise SystemExit(0 if result["passed"] else 2)
