"""Evidence and cleanup for one combined GPU-student / Mobilint-teacher training run.

The NPU teacher Pod is a task-owned auxiliary resource: the Resource Advisor job
reserves only the GPU, so this script records the teacher's own reservation
(``mobilint.com/npu``), lifecycle timestamps, image and request counter, links
them to the RA job's native view, and deletes the teacher Pod only by its UID.

    python examples/run_heterogeneous_training.py evidence --job-id J --out FILE
    python examples/run_heterogeneous_training.py cleanup --out FILE

Read-only except ``cleanup`` (one Pod deletion, UID-checked). No secrets are
read or written; the RA API is reached through the existing LAN console proxy.
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime

NAMESPACE = "topology-profile-lab"
POD = "ra-hetero-kd-teacher-20261029"
API = "http://10.254.192.47:18420/api/v1/compute"


def kubectl(*args):
    return subprocess.run(
        ["kubectl", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout


def teacher_pod():
    pod = json.loads(kubectl("-n", NAMESPACE, "get", "pod", POD, "-o", "json"))
    status, spec = pod["status"], pod["spec"]
    container = spec["containers"][0]
    conditions = {c["type"]: c.get("lastTransitionTime") for c in status.get("conditions", [])}
    cstatus = (status.get("containerStatuses") or [{}])[0]
    return {
        "namespace": NAMESPACE,
        "name": POD,
        "uid": pod["metadata"]["uid"],
        "node_name": spec.get("nodeName"),
        "pod_ip": status.get("podIP"),
        "phase": status.get("phase"),
        "image": container["image"],
        "image_id": cstatus.get("imageID"),
        "reservation": container["resources"],
        "created_at": pod["metadata"]["creationTimestamp"],
        "scheduled_at": conditions.get("PodScheduled"),
        "ready_at": conditions.get("Ready"),
        "started_at": (cstatus.get("state", {}).get("running") or {}).get("startedAt"),
        "restart_count": cstatus.get("restartCount"),
    }


def teacher_health(ip):
    with urllib.request.urlopen(f"http://{ip}:8080/v1/teacher/health", timeout=10) as r:
        return json.loads(r.read())


def npu_holders():
    pods = json.loads(kubectl("get", "pods", "-A", "-o", "json"))["items"]
    return [
        {
            "namespace": p["metadata"]["namespace"],
            "name": p["metadata"]["name"],
            "uid": p["metadata"]["uid"],
            "node": p["spec"].get("nodeName"),
        }
        for p in pods
        if p["status"].get("phase") not in {"Succeeded", "Failed"}
        and any(
            "mobilint.com/npu" in ((c.get("resources") or {}).get("limits") or {})
            for c in p["spec"]["containers"]
        )
    ]


def job_view(job_id):
    with urllib.request.urlopen(f"{API}/jobs/{job_id}/view", timeout=30) as r:
        return json.loads(r.read())


def evidence(args):
    pod = teacher_pod()
    value = {
        "schema": "ra-heterogeneous-training-evidence-v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "claims": (
            "One combined training application: the GPU student is the only model whose "
            "weights change; the Mobilint NPU runs the frozen teacher forward. The RA job "
            "reserves the GPU; the NPU is reserved by the separate task-owned teacher Pod "
            "recorded here."
        ),
        "teacher_pod": pod,
        "teacher_health": teacher_health(pod["pod_ip"]) if pod["phase"] == "Running" else None,
        "npu_holders": npu_holders(),
        "jobs": [job_view(j) for j in args.job_id],
    }
    with open(args.out, "w") as f:
        json.dump(value, f, indent=1)
    print(
        json.dumps(
            {
                "teacher_uid": pod["uid"],
                "requests": (value["teacher_health"] or {}).get("requests"),
                "jobs": [(j["job_id"], j["state"]) for j in value["jobs"]],
            }
        )
    )


def cleanup(args):
    with open(args.out) as f:
        recorded = json.load(f)["teacher_pod"]["uid"]
    current = json.loads(kubectl("-n", NAMESPACE, "get", "pod", POD, "-o", "json"))["metadata"][
        "uid"
    ]
    if current != recorded:
        raise SystemExit(f"refusing: live UID {current} != recorded {recorded}")
    kubectl("-n", NAMESPACE, "delete", "pod", POD, "--wait=true", "--timeout=60s")
    print(json.dumps({"deleted_uid": recorded, "npu_holders_after": npu_holders()}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("evidence")
    e.add_argument("--job-id", action="append", default=[])
    e.add_argument("--out", required=True)
    c = sub.add_parser("cleanup")
    c.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    {"evidence": evidence, "cleanup": cleanup}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
