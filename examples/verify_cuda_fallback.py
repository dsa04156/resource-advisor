"""Two bounded Kueue qualification Jobs; reports/config are private.

Config: lab_only, namespace, base_manifest (qualified Job file), database_url,
run_ref. No API registration/ingestion. See docs/cuda-fallback-plan.md.
"""

import argparse
import copy
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from resource_advisor.contracts import ExecutionResult, signature
from resource_advisor.store import Store, entities, jobs, outbox, usage

WRAPPER = """import json,torch,runpy
print('CUDA_VISIBILITY '+json.dumps({'pytorch':torch.__version__,'cuda':torch.version.cuda,
'cuda_available':torch.cuda.is_available(),'device_count':torch.cuda.device_count()}),flush=True)
runpy.run_module('resource_advisor.gpu_benchmark',run_name='__main__')
"""


def manifest(base, run_ref, negative):
    value = copy.deepcopy(base)
    name = run_ref + ("-negative" if negative else "-positive")
    value["metadata"]["name"] = name
    value["metadata"]["annotations"] = {"resource-advisor/job-id": name}
    value["spec"].update(suspend=True, backoffLimit=0, activeDeadlineSeconds=90)
    pod = value["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False and not pod.get("nodeName")
    assert len(pod["containers"]) == 1 and pod["restartPolicy"] == "Never"
    assert not pod.get("initContainers") and not pod.get("hostNetwork")
    c = pod["containers"][0]
    assert "@sha256:" in c["image"] and all(m.get("readOnly") for m in c["volumeMounts"])
    assert all("hostPath" not in v for v in pod["volumes"])
    assert not c.get("envFrom") and all("value" in e for e in c["env"])
    assert not c["securityContext"].get("allowPrivilegeEscalation")
    assert not c["securityContext"].get("privileged")
    env = {e["name"]: e["value"] for e in c["env"]}
    context = json.loads(env["RA_CONTEXT_JSON"])
    context["runtime_versions"] = {k: context["runtime_versions"][k] for k in ("pytorch", "cuda")}
    context["resources"].update(
        host_cpu=1, host_memory_mib=2048, accelerator_count=0 if negative else 1
    )
    env.update(
        RA_JOB_ID=name,
        RA_ATTEMPT_ID=name,
        RA_EPOCH="1",
        RA_CONTEXT_JSON=json.dumps(context),
        RA_CONTEXT_SIGNATURE=signature(context),
        RA_INPUT_SHAPE="[256,256]",
        RA_WORK_UNITS="20",
        RA_PRECISION="fp32",
        RA_SEED="0",
        RA_EXECUTION_MODE="qualification",
        RA_WORKLOAD_SIGNATURE=signature(
            {
                "source": "gpu_benchmark.py",
                "shape": [256, 256],
                "precision": "fp32",
                "seed": 0,
                "iterations": 20,
            }
        ),
    )
    for key in ("RA_THERMAL_POLICY_JSON", "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES"):
        env.pop(key, None)
    if negative:
        env.update(NVIDIA_VISIBLE_DEVICES="void", CUDA_VISIBLE_DEVICES="")
    c["env"] = [{"name": key, "value": val} for key, val in env.items()]
    c["command"] = ["python", "-c", WRAPPER]
    resources = {"cpu": "1", "memory": "2048Mi"}
    if not negative:
        resources["nvidia.com/gpu"] = "1"
    c["resources"] = {"requests": resources, "limits": dict(resources)}
    return value


def run(config, directory):
    assert config["lab_only"] is True
    assert re.fullmatch(r"ra-[a-z0-9-]{1,40}", config["run_ref"])
    directory.mkdir(mode=0o700)
    state = {"status": "INCOMPLETE", "attempts": {}, "checks": {}}

    def save(name, value):
        p = directory / name
        p.write_text(json.dumps(value, indent=2) + "\n")
        p.chmod(0o600)

    def checkpoint():
        save("report.json", state)

    def kube(*args):
        result = subprocess.run(
            ["rtk", "proxy", "kubectl", "--request-timeout=10s", "-n", config["namespace"], *args],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            save(
                "native-error.json",
                {"argv": args, "stderr": result.stderr, "exit": result.returncode},
            )
            raise RuntimeError("native command failed; inspect retained IDs, never restart trial")
        return result.stdout

    def get(kind, name=None):
        return json.loads(kube("get", kind, *([name] if name else []), "-o", "json"))

    def preserved_objects():
        values = {}
        for kind in (
            "jobs",
            "pods",
            "workloads.kueue.x-k8s.io",
            "deployments",
            "persistentvolumeclaims",
        ):
            values[kind] = {
                row["metadata"]["uid"]: signature(row["spec"]) for row in get(kind)["items"]
            }
        return values

    store = Store(config["database_url"])

    def metadata_snapshot():
        with store.transaction() as conn:
            counts = {
                table.name: conn.scalar(select(func.count()).select_from(table))
                for table in (jobs, outbox, usage)
            }
            immutable = [
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
            "immutable_records_sha256": signature(immutable),
            "immutable_record_count": len(immutable),
        }

    checkpoint()
    base = json.loads(Path(config["base_manifest"]).read_text())
    assert base["metadata"]["namespace"] == config["namespace"]
    pod = base["spec"]["template"]["spec"]
    node = get("node", pod["nodeSelector"]["kubernetes.io/hostname"])
    conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
    assert conditions["Ready"] == "True" and all(
        conditions.get(k) == "False" for k in ("MemoryPressure", "DiskPressure", "PIDPressure")
    )
    assert all(
        any(
            c["type"] in {"Complete", "Failed"} and c["status"] == "True"
            for c in j.get("status", {}).get("conditions", [])
        )
        for j in get("jobs")["items"]
    )
    source_name = next(
        v["configMap"]["name"] for v in pod["volumes"] if v["name"] == "qualified-source"
    )
    source = get("configmap", source_name)
    code = source["data"]["gpu_benchmark.py"]
    assert (
        code
        == Path(__file__).parents[1].joinpath("src/resource_advisor/gpu_benchmark.py").read_text()
    )
    state["source_sha256"] = hashlib.sha256(code.encode()).hexdigest()
    state["source_before"] = signature(source["data"])
    state["preservation_before"] = preserved_objects()
    state["metadata_before"] = metadata_snapshot()
    checkpoint()
    for negative in (False, True):
        label = "negative" if negative else "positive"
        desired = manifest(base, config["run_ref"], negative)
        path = directory / (label + "-manifest.json")
        save(path.name, desired)
        state["attempts"][label] = {"intent": desired["metadata"]["name"]}
        checkpoint()
        accepted = json.loads(kube("create", "-f", str(path), "-o", "json"))
        name = accepted["metadata"]["name"]
        state["attempts"][label]["job_uid"] = accepted["metadata"]["uid"]
        checkpoint()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = get("job", name)
            state["attempts"][label]["latest"] = current
            checkpoint()
            if any(
                c["type"] in {"Complete", "Failed"} and c["status"] == "True"
                for c in current.get("status", {}).get("conditions", [])
            ):
                break
            time.sleep(1)
        else:
            raise TimeoutError("inspect same accepted Job, do not create replacement")
        pods = json.loads(kube("get", "pods", "-l", "job-name=" + name, "-o", "json"))["items"]
        assert len(pods) == 1
        observed_pod = pods[0]
        logs = kube("logs", observed_pod["metadata"]["name"], "-c", "workload")
        workloads = [
            w
            for w in get("workloads.kueue.x-k8s.io")["items"]
            if any(
                o.get("uid") == accepted["metadata"]["uid"]
                for o in w["metadata"].get("ownerReferences", [])
            )
        ]
        assert len(workloads) == 1
        terminated = observed_pod["status"]["containerStatuses"][0]["state"]["terminated"]
        scheduled = next(
            c["lastTransitionTime"]
            for c in observed_pod["status"]["conditions"]
            if c["type"] == "PodScheduled" and c["status"] == "True"
        )
        seconds = (
            datetime.fromisoformat(terminated["finishedAt"].replace("Z", "+00:00"))
            - datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        ).total_seconds()
        visibility = [
            json.loads(line[len("CUDA_VISIBILITY ") :])
            for line in logs.splitlines()
            if line.startswith("CUDA_VISIBILITY ")
        ]
        assert len(visibility) == 1
        result_lines = [
            line for line in logs.splitlines() if line.startswith("RESOURCE_ADVISOR_RESULT ")
        ]
        item = state["attempts"][label]
        item.update(
            pod=observed_pod,
            logs=logs,
            workload=workloads[0],
            visibility=visibility[0],
            reservation_seconds=seconds,
            cpu_core_reservation_seconds=seconds,
            gpu_reservation_seconds=0 if negative else seconds,
        )
        checkpoint()
        assert (
            seconds >= 0
            and visibility[0]["pytorch"] == "2.8.0+cu128"
            and visibility[0]["cuda"] == "12.8"
        )
        assert observed_pod["status"]["containerStatuses"][0]["restartCount"] == 0
        assert any(
            c["type"] == "Admitted" and c["status"] == "True"
            for c in workloads[0]["status"]["conditions"]
        )
        if negative:
            assert not visibility[0]["cuda_available"] and visibility[0]["device_count"] == 0
            assert terminated["exitCode"] == 1 and "CUDA unavailable; refusing CPU fallback" in logs
            assert not result_lines and current["status"].get("failed") == 1
            assert (
                "nvidia.com/gpu"
                not in observed_pod["spec"]["containers"][0]["resources"]["requests"]
            )
        else:
            assert visibility[0]["cuda_available"] and visibility[0]["device_count"] == 1
            assert terminated["exitCode"] == 0 and current["status"].get("succeeded") == 1
            assert len(result_lines) == 1
            envelope = json.loads(result_lines[0][len("RESOURCE_ADVISOR_RESULT ") :])
            result = ExecutionResult.model_validate(envelope["result"])
            assert signature(result) == envelope["digest"]
            assert (
                result.job_id == result.attempt_id == name
                and result.measurements.quality_value == 1.0
            )
            item["result"] = envelope
        checkpoint()
        print(label, "passed; GPU reservation seconds", item["gpu_reservation_seconds"], flush=True)
    state["metadata_after"] = metadata_snapshot()
    state["source_after"] = signature(get("configmap", source_name)["data"])
    state["preservation_after"] = preserved_objects()
    checkpoint()
    assert (
        state["metadata_before"] == state["metadata_after"]
        and state["source_before"] == state["source_after"]
    )
    for kind, existing in state["preservation_before"].items():
        assert all(
            state["preservation_after"][kind].get(uid) == digest for uid, digest in existing.items()
        ), kind
    state["checks"] = {
        "api_metadata_unchanged": True,
        "existing_native_objects_preserved": True,
        "qualified_source_unchanged": True,
        "two_terminal_test_jobs": True,
        "no_cpu_fallback_result": True,
    }
    state["status"] = "PASS"
    checkpoint()
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    result = run(json.loads(args.config.read_text()), args.directory)
    print(result["status"], "two retained qualification attempts; no API profiles registered")
