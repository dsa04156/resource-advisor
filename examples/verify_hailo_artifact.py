"""Two real lab Jobs for docs/reference/hailo-artifact.md; keep config/report private."""

import argparse
import copy
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from resource_advisor.contracts import ExecutionResult, signature
from resource_advisor.hailo_qualification import quality_gates
from resource_advisor.store import Store, entities, jobs, outbox, usage

WRAPPER = """import hashlib,json,pathlib,runpy,sys
sha=lambda p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
print('HAILO_ARTIFACT '+json.dumps({'hef_sha256':sha(sys.argv[-1]),
'binding_sha256':sha('/opt/platform-binding.json'),
'sources':{n:sha('/opt/resource-advisor/resource_advisor/'+n) for n in
['hailo_qualification.py','hailo_benchmark.py']}}),flush=True)
runpy.run_module('resource_advisor.hailo_benchmark',run_name='__main__')
"""
COPY = """import hashlib,pathlib,shutil
p=pathlib.Path('/opt/fixture/model.hef')
print('ALTERNATE_HEF '+hashlib.sha256(p.read_bytes()).hexdigest(),flush=True)
shutil.copyfile(p,'/alternate/model.hef')
"""
EXPECTED = "a1d82e9121c66e772257490cb3af904d1e90fb4387ad5f683fbc5efe1a05f9f7"
ALTERNATE = "d5a76ed6f116fc9b9ef502df914a298917091724ed88cfccab36bfb716f61b22"


def manifest(base, config, negative):
    name = config["run_ref"] + ("-negative" if negative else "-positive")
    pod = copy.deepcopy(base["spec"]["template"]["spec"])
    assert len(pod["containers"]) == 1 and not pod.get("volumes")
    assert not pod.get("nodeName") and not pod.get("initContainers")
    assert pod["automountServiceAccountToken"] is False and pod["restartPolicy"] == "Never"
    pod["securityContext"].update(runAsNonRoot=True, runAsUser=10001, fsGroup=10001)
    pod["volumes"] = [{"name": "alternate", "emptyDir": {"sizeLimit": "32Mi"}}]
    c = pod["containers"][0]
    assert "@sha256:" in c["image"] and "@sha256:" in config["alternate_image"]
    assert not c.get("envFrom") and all("value" in e for e in c["env"])
    assert (
        c["resources"]["requests"]
        == c["resources"]["limits"]
        == {"cpu": "1", "memory": "1Gi", "hailo.ai/h8": "1"}
    )
    for e in c["env"]:
        if e["name"] in {"RA_JOB_ID", "RA_ATTEMPT_ID"}:
            e["value"] = name
        if e["name"] == "RA_EXECUTION_MODE":
            e["value"] = "observe"
    c["command"] = [
        "python",
        "-c",
        WRAPPER,
        "--binding",
        "/opt/platform-binding.json",
        "--fixture",
        "/opt/fixture",
        "--hef",
        "/alternate/model.hef" if negative else "/opt/fixture/model.hef",
    ]
    c["volumeMounts"] = [{"name": "alternate", "mountPath": "/alternate", "readOnly": True}]
    security = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    c["securityContext"] = security
    pod["initContainers"] = [
        {
            "name": "copy-alternate",
            "image": config["alternate_image"],
            "command": ["python", "-c", COPY],
            "securityContext": security,
            "resources": {
                "requests": {"cpu": "1", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "256Mi"},
            },
            "volumeMounts": [{"name": "alternate", "mountPath": "/alternate"}],
        }
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": config["namespace"],
            "labels": {"kueue.x-k8s.io/queue-name": config["queue"]},
        },
        "spec": {
            "suspend": True,
            "backoffLimit": 0,
            "activeDeadlineSeconds": 90,
            "template": {"spec": pod},
        },
    }


def run(config, directory):
    assert config["lab_only"] is True and re.fullmatch(r"ra-[a-z0-9-]{1,40}", config["run_ref"])
    directory.mkdir(mode=0o700)
    state = {"status": "INCOMPLETE", "attempts": {}}

    def save(name, value):
        path = directory / name
        path.write_text(json.dumps(value, indent=2) + "\n")
        path.chmod(0o600)

    def checkpoint():
        save("report.json", state)

    def kube(*args):
        r = subprocess.run(
            ["rtk", "proxy", "kubectl", "--request-timeout=10s", "-n", config["namespace"], *args],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if r.returncode:
            save("native-error.json", {"argv": args, "stderr": r.stderr, "exit": r.returncode})
            raise RuntimeError("inspect original accepted IDs; no replacement compute")
        return r.stdout

    def get(kind, name=None):
        return json.loads(kube("get", kind, *([name] if name else []), "-o", "json"))

    def objects():
        return {
            kind: {r["metadata"]["uid"]: signature(r["spec"]) for r in get(kind)["items"]}
            for kind in (
                "jobs",
                "pods",
                "workloads.kueue.x-k8s.io",
                "deployments",
                "persistentvolumeclaims",
                "clusterqueues.kueue.x-k8s.io",
            )
        }

    store = Store(config["database_url"])

    def metadata():
        with store.transaction() as conn:
            counts = {
                t.name: conn.scalar(select(func.count()).select_from(t))
                for t in (jobs, outbox, usage)
            }
            rows = [
                dict(r)
                for r in conn.execute(
                    select(entities)
                    .where(
                        entities.c.kind.in_(
                            ["result", "profile", "tracking", "artifact", "artifact_tracking"]
                        )
                    )
                    .order_by(entities.c.kind, entities.c.ref)
                ).mappings()
            ]
        return {
            "counts": counts,
            "immutable_records_sha256": signature(rows),
            "immutable_record_count": len(rows),
        }

    checkpoint()
    base = json.loads(Path(config["base_manifest"]).read_text())
    node = get("node", base["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"])
    conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
    assert conditions["Ready"] == "True" and node["status"]["allocatable"]["hailo.ai/h8"] == "1"
    assert all(
        conditions.get(c) == "False" for c in ("MemoryPressure", "DiskPressure", "PIDPressure")
    )
    assert all(
        any(
            c["type"] in {"Complete", "Failed"} and c["status"] == "True"
            for c in j.get("status", {}).get("conditions", [])
        )
        for j in get("jobs")["items"]
    )
    queue = get("localqueue", config["queue"])
    assert (
        queue["status"].get("pendingWorkloads", 0)
        == queue["status"].get("admittedWorkloads", 0)
        == 0
    )
    state.update(metadata_before=metadata(), native_before=objects())
    checkpoint()
    for negative in (False, True):
        label = "negative" if negative else "positive"
        desired = manifest(base, config, negative)
        save(label + "-manifest.json", desired)
        state["attempts"][label] = {"intent": desired["metadata"]["name"]}
        checkpoint()
        accepted = json.loads(
            kube("create", "-f", str(directory / (label + "-manifest.json")), "-o", "json")
        )
        name = accepted["metadata"]["name"]
        item = state["attempts"][label]
        item["job_uid"] = accepted["metadata"]["uid"]
        checkpoint()
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            current = get("job", name)
            item["job"] = current
            checkpoint()
            if any(
                c["type"] in {"Complete", "Failed"} and c["status"] == "True"
                for c in current.get("status", {}).get("conditions", [])
            ):
                break
            time.sleep(1)
        else:
            raise TimeoutError("inspect same accepted IDs; no replacement Job")
        pods = json.loads(kube("get", "pods", "-l", "job-name=" + name, "-o", "json"))["items"]
        assert len(pods) == 1
        pod = pods[0]
        main = pod["status"]["containerStatuses"][0]
        init = pod["status"]["initContainerStatuses"][0]
        logs = kube("logs", pod["metadata"]["name"], "-c", "workload")
        init_logs = kube("logs", pod["metadata"]["name"], "-c", "copy-alternate")
        ws = [
            w
            for w in get("workloads.kueue.x-k8s.io")["items"]
            if any(
                o.get("uid") == accepted["metadata"]["uid"]
                for o in w["metadata"].get("ownerReferences", [])
            )
        ]
        assert len(ws) == 1
        term = main["state"]["terminated"]
        scheduled = next(
            c["lastTransitionTime"]
            for c in pod["status"]["conditions"]
            if c["type"] == "PodScheduled" and c["status"] == "True"
        )
        seconds = (
            datetime.fromisoformat(term["finishedAt"].replace("Z", "+00:00"))
            - datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        ).total_seconds()
        item.update(
            pod=pod,
            logs=logs,
            init_logs=init_logs,
            workload=ws[0],
            npu_reservation_seconds=seconds,
            cpu_core_reservation_seconds=seconds,
        )
        checkpoint()
        assert seconds >= 0 and main["restartCount"] == init["restartCount"] == 0
        assert (
            init["state"]["terminated"]["exitCode"] == 0
            and init_logs.strip() == "ALTERNATE_HEF " + ALTERNATE
        )
        assert any(
            c["type"] == "Admitted" and c["status"] == "True" for c in ws[0]["status"]["conditions"]
        )

        def markers(prefix, text=logs):
            return [
                json.loads(line[len(prefix) :])
                for line in text.splitlines()
                if line.startswith(prefix)
            ]

        artifacts = markers("HAILO_ARTIFACT ")
        assert len(artifacts) == 1
        artifact = artifacts[0]
        assert artifact["hef_sha256"] == (ALTERNATE if negative else EXPECTED)
        assert artifact["binding_sha256"] == config["binding_sha256"]
        assert artifact["sources"] == config["source_sha256"]
        item["artifact"] = artifact
        results, reports = (
            markers("RESOURCE_ADVISOR_RESULT "),
            markers("RESOURCE_ADVISOR_HAILO_REPORT "),
        )
        if negative:
            assert term["exitCode"] == 1 and current["status"].get("failed") == 1
            assert "compiled model digest mismatch" in logs and not results and not reports
        else:
            assert term["exitCode"] == 0 and current["status"].get("succeeded") == 1
            assert len(results) == len(reports) == 1
            result = ExecutionResult.model_validate(results[0]["result"])
            assert result.job_id == result.attempt_id == name and result.outcome == "COMPLETED"
            assert (
                result.epoch == 1
                and result.evidence_kind == "hardware"
                and signature(result) == results[0]["digest"]
            )
            report = reports[0]
            rows = report["predictions"]
            quality = quality_gates(
                [r["prediction"] for r in rows],
                [r["label"] for r in rows],
                [r["reference_top1"] for r in rows],
            )
            assert report["quality"] == quality and quality["qualified"]
            assert report["measured_images"] == len(rows) == result.measurements.sample_count == 100
            assert report["architecture"] == "HAILO8" and report["device_count"] == 1
            assert report["hailort"] == report["driver"] == "4.23.0"
            item.update(result=results[0], report=report)
        checkpoint()
        print(label, "verified; NPU reservation seconds", seconds, flush=True)
    state.update(
        metadata_after=metadata(),
        native_after=objects(),
        queue_after=get("localqueue", config["queue"])["status"],
    )
    checkpoint()
    assert state["metadata_before"] == state["metadata_after"]
    for kind, existing in state["native_before"].items():
        assert all(
            state["native_after"][kind].get(uid) == digest for uid, digest in existing.items()
        ), kind
    assert (
        state["queue_after"].get("pendingWorkloads", 0)
        == state["queue_after"].get("admittedWorkloads", 0)
        == 0
    )
    state["status"] = "PASS"
    checkpoint()
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    report = run(json.loads(args.config.read_text()), args.directory)
    print(report["status"], "two retained attempts; no API profiles registered")
