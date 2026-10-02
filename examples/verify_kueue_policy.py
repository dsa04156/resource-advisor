"""Verify an existing isolated one-GPU Kueue lab using a qualified GPU Job template.

The template command must run a real CUDA probe and print a JSON F0 PASS record.
No cluster policy is mutated. Only this invocation's Jobs are deleted. Private
reports contain site identifiers and must be sanitized before publication.
"""

import argparse
import copy
import json
import subprocess
import time
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("template", "namespace", "queue", "cluster-queue", "report"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--normal", default="ra-policy-normal")
    parser.add_argument("--high", default="ra-policy-high")
    args = parser.parse_args()
    report = Path(args.report)
    with report.open("x"):
        pass
    report.chmod(0o600)
    template = json.loads(Path(args.template).read_text())
    assert template["kind"] == "Job"
    evidence = {"jobs": {}, "checks": {}, "result": "INCOMPLETE"}
    owned = []
    prefix = "ra-queue-" + uuid.uuid4().hex[:8]

    def save():
        report.write_text(json.dumps(evidence, indent=2) + "\n")

    def kubectl(*argv, obj=None):
        result = subprocess.run(
            ["kubectl", "--namespace", args.namespace, *argv],
            input=json.dumps(obj) if obj else None,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        return result.stdout

    def get(kind, name=None, *more):
        return json.loads(kubectl("get", kind, *([name] if name else []), *more, "-o", "json"))

    def until(fn, limit=100):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            value = fn()
            if value:
                return value
            time.sleep(1)
        raise RuntimeError("observation deadline exceeded; inspect private report and owned Jobs")

    def create(role, priority, hold=0, gpu=1):
        job = copy.deepcopy(template)
        name = prefix + "-" + role
        job["metadata"] = {
            "name": name,
            "namespace": args.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "resource-advisor-verification",
                "kueue.x-k8s.io/queue-name": args.queue,
                "kueue.x-k8s.io/priority-class": priority,
            },
        }
        spec = job["spec"]
        spec.update(
            suspend=True, backoffLimit=0, activeDeadlineSeconds=180, ttlSecondsAfterFinished=600
        )
        pod = spec["template"]["spec"]
        assert "nodeName" not in pod and "priorityClassName" not in pod
        assert len(pod["containers"]) == 1 and not pod.get("initContainers")
        container = pod["containers"][0]
        assert container["command"][:2] == ["bash", "-c"]
        container["command"][2] = "set -e; " + container["command"][2] + f"; sleep {hold}"
        for kind in ("requests", "limits"):
            container["resources"][kind]["nvidia.com/gpu"] = str(gpu)
        owned.append(name)
        evidence["jobs"][role] = {"name": name, "manifest": job}
        save()
        created = json.loads(kubectl("create", "-f", "-", "-o", "json", obj=job))
        evidence["jobs"][role]["uid"] = created["metadata"]["uid"]
        save()
        return name

    def workload(role):
        uid = evidence["jobs"][role]["uid"]
        items = get("workloads")["items"]
        found = [
            w
            for w in items
            if any(r["uid"] == uid for r in w["metadata"].get("ownerReferences", []))
        ]
        assert len(found) <= 1
        return found[0] if found else None

    def condition(w, kind):
        return next((c for c in w.get("status", {}).get("conditions", []) if c["type"] == kind), {})

    def pending(role):
        w = workload(role)
        if not w or condition(w, "QuotaReserved").get("status") != "False":
            return None
        job = get("job", evidence["jobs"][role]["name"])
        pods = get("pods", None, "-l", "job-name=" + job["metadata"]["name"])["items"]
        assert job["spec"]["suspend"] is True and not pods, "unadmitted workload created Pods"
        evidence["jobs"][role]["pending"] = w
        save()
        return w

    try:
        cq = get("clusterqueue", args.cluster_queue)
        assert (
            cq["status"].get("pendingWorkloads", 0) == cq["status"].get("admittedWorkloads", 0) == 0
        )
        assert cq["spec"]["preemption"]["withinClusterQueue"] == "Never"
        assert get("localqueue", args.queue)["spec"]["clusterQueue"] == args.cluster_queue
        evidence["cluster_queue_before"] = cq
        over = create("oversized", args.normal, gpu=2)
        w = until(lambda: pending("oversized"))
        assert "quota" in condition(w, "QuotaReserved").get("message", "").lower()
        evidence["checks"]["oversized_stays_suspended_without_pods"] = True
        kubectl("delete", "job", over, "--wait=true", "--timeout=30s")
        blocker = create("blocker", args.normal, hold=120)
        until(
            lambda: any(
                p["status"]["phase"] == "Running"
                for p in get("pods", None, "-l", "job-name=" + blocker)["items"]
            )
        )
        low = create("normal", args.normal, hold=5)
        until(lambda: pending("normal"))
        high = create("high", args.high, hold=5)
        until(lambda: pending("high"))
        assert (
            evidence["jobs"]["high"]["pending"]["spec"]["priority"]
            > evidence["jobs"]["normal"]["pending"]["spec"]["priority"]
        )
        evidence["blocker_log"] = kubectl("logs", "job/" + blocker)
        kubectl("delete", "job", blocker, "--wait=true", "--timeout=30s", "--cascade=foreground")
        until(lambda: condition(workload("high") or {}, "Admitted").get("status") == "True")
        assert condition(workload("normal"), "Admitted").get("status") != "True"
        evidence["checks"]["later_high_admitted_before_older_normal"] = True
        for role, name in (("high", high), ("normal", low)):
            until(lambda name=name: get("job", name).get("status", {}).get("succeeded") == 1)
            entry = evidence["jobs"][role]
            entry["finished_job"] = get("job", name)
            entry["finished_workload"] = workload(role)
            entry["log"] = kubectl("logs", "job/" + name)
            records = [
                json.loads(line) for line in entry["log"].splitlines() if line.startswith("{")
            ]
            assert any(
                r.get("result") == "PASS"
                and r.get("cuda_devices") == 1
                and r.get("verified_elements") == 4096
                for r in records
            )
            save()
        starts = [
            evidence["jobs"][r]["finished_job"]["status"]["startTime"] for r in ("high", "normal")
        ]
        assert starts[0] < starts[1]
        evidence["checks"]["real_cuda_results_both_pass"] = True
        evidence["checks"]["high_execution_started_first"] = True
        evidence["result"] = "PASS"
    finally:
        errors = []
        for name in owned:
            try:
                kubectl(
                    "delete",
                    "job",
                    name,
                    "--ignore-not-found",
                    "--wait=true",
                    "--timeout=30s",
                    "--cascade=foreground",
                )
            except subprocess.SubprocessError as exc:
                errors.append(str(exc))
        evidence["cleanup_errors"] = errors
        save()
        print(
            json.dumps(
                {
                    "result": evidence["result"],
                    "checks": evidence["checks"],
                    "cleanup_errors": errors,
                }
            )
        )
        assert not errors


if __name__ == "__main__":
    main()
