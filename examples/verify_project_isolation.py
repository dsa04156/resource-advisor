"""Bounded E6 API/Kueue acceptance; requires the frozen project's private config.

Config holds api_url, ca_file, run_ref, cluster_queue and projects a/b. Each
project has project_ref, token, namespace, local_queue, candidate_ref and workload
refs normal/high/oversized_cpu/oversized_gpu. Never publish config or raw reports.
Provisioning, qualifications and artifact readback are separate explicit steps.
"""

import argparse
import json
import ssl
import subprocess
import time
from pathlib import Path

import httpx

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "RESULT_INVALID"}


def condition(workload, kind):
    return next(
        (c for c in workload.get("status", {}).get("conditions", []) if c["type"] == kind), {}
    )


def run(config, report):
    with report.open("x"):
        pass
    report.chmod(0o600)
    state = {"result": "INCOMPLETE", "jobs": {}, "checks": {}, "rounds": []}
    clients = {
        key: httpx.Client(
            base_url=config["api_url"],
            verify=ssl.create_default_context(cafile=config["ca_file"]),
            headers={"Authorization": "Bearer " + value["token"]},
            timeout=20,
        )
        for key, value in config["projects"].items()
    }
    assert set(clients) == {"a", "b"}

    def save():
        report.write_text(json.dumps(state, indent=2) + "\n")

    def api(project, method, path, expected=200, **kwargs):
        response = clients[project].request(method, "/api/v1/compute" + path, **kwargs)
        assert response.status_code == expected, (
            project,
            path,
            response.status_code,
            response.text,
        )
        return response.json()

    def kube(project, kind, name=None, *more):
        command = ["kubectl", "--request-timeout=15s"]
        if config.get("kubeconfig"):
            command += ["--kubeconfig", config["kubeconfig"]]
        command += ["-n", config["projects"][project]["namespace"], "get", kind]
        command += ([name] if name else []) + list(more) + ["-o", "json"]
        return json.loads(subprocess.check_output(command, text=True, timeout=20))

    def until(function, seconds=150):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = function()
            if value:
                return value
            time.sleep(0.5)
        raise TimeoutError("inspect recorded attempts and scheduler state; do not resubmit")

    def request(project, role):
        cfg = config["projects"][project]
        return {
            "workload_ref": cfg["workloads"][role],
            "candidate_ref": cfg["candidate_ref"],
            "mode": "observe",
        }

    def submit(label, project, role, key):
        payload = request(project, role)
        headers = {"Idempotency-Key": config["run_ref"] + "-" + key}
        value = api(project, "POST", "/jobs", json=payload, headers=headers)
        state["jobs"][label] = {"project": project, "request": payload, "job": value}
        save()
        again = api(project, "POST", "/jobs", json=payload, headers=headers)
        assert again["job_id"] == value["job_id"] and again["attempt_id"] == value["attempt_id"]
        return value

    def snapshot(label):
        entry = state["jobs"][label]
        project, identity = entry["project"], entry["job"]
        current = api(project, "GET", "/jobs/" + identity["job_id"])
        if not current["external_id"]:
            assert current["state"] not in TERMINAL, current
            return None
        cfg = config["projects"][project]
        job = kube(project, "job", current["external_id"])
        assert job["metadata"]["namespace"] == cfg["namespace"]
        assert job["metadata"]["annotations"]["resource-advisor/job-id"] == identity["job_id"]
        assert job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == cfg["local_queue"]
        pod_spec = job["spec"]["template"]["spec"]
        assert pod_spec["automountServiceAccountToken"] is False and "nodeName" not in pod_spec
        assert pod_spec["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] == "1"
        workloads = [
            w
            for w in kube(project, "workloads")["items"]
            if any(
                o.get("uid") == job["metadata"]["uid"]
                for o in w["metadata"].get("ownerReferences", [])
            )
        ]
        assert len(workloads) <= 1
        if not workloads:
            return None
        workload = workloads[0]
        assert workload["spec"]["queueName"] == cfg["local_queue"]
        if workload.get("status", {}).get("admission"):
            assert workload["status"]["admission"]["clusterQueue"] == config["cluster_queue"]
        pods = kube(project, "pods", None, "-l", "job-name=" + current["external_id"])["items"]
        value = {"api": current, "job": job, "workload": workload, "pods": pods}
        entry["latest"] = value
        save()
        return value

    def pending(label):
        sample = snapshot(label)
        if sample and condition(sample["workload"], "QuotaReserved").get("status") == "False":
            assert sample["job"]["spec"]["suspend"] and not sample["pods"]
            assert (
                "quota" in condition(sample["workload"], "QuotaReserved").get("message", "").lower()
            )
            state["jobs"][label]["pending"] = sample
            save()
            return sample
        return None

    def active(label):
        sample = snapshot(label)
        return (
            sample
            if sample and any(p["status"]["phase"] == "Running" for p in sample["pods"])
            else None
        )

    def terminal(label):
        entry = state["jobs"][label]
        value = api(entry["project"], "GET", "/jobs/" + entry["job"]["job_id"])
        if value["state"] in TERMINAL:
            entry["terminal"] = value
            save()
            return value
        return None

    try:
        for project in clients:
            assert all(j["state"] in TERMINAL for j in api(project, "GET", "/jobs"))
            for field in ("namespace", "local_queue", "project_ref"):
                api(
                    project,
                    "POST",
                    "/jobs",
                    expected=422,
                    json={**request(project, "normal"), field: "foreign"},
                    headers={"Idempotency-Key": config["run_ref"] + "-override"},
                )
            api(
                project,
                "POST",
                "/jobs",
                expected=422,
                json=request(project, "oversized_gpu"),
                headers={"Idempotency-Key": config["run_ref"] + "-gpu-oversized"},
            )
            other = "b" if project == "a" else "a"
            api(
                project,
                "POST",
                "/jobs",
                expected=404,
                json=request(other, "normal"),
                headers={"Idempotency-Key": config["run_ref"] + "-foreign-workload"},
            )
            label = project + "-oversized-cpu"
            submit(label, project, "oversized_cpu", "cpu-oversized")
            until(lambda label=label: pending(label))
            api(project, "POST", "/jobs/" + state["jobs"][label]["job"]["job_id"] + "/cancel")
            assert until(lambda label=label: terminal(label))["state"] == "CANCELED"
        state["checks"]["request_scope_and_quota_negatives"] = True
        save()
        print(
            "scope overrides, foreign workload, GPU capacity and queued CPU quota checks passed",
            flush=True,
        )

        for number, (first, second) in enumerate((("a", "b"), ("b", "a")), start=1):
            holder, normal, high = [
                f"round-{number}-" + role for role in ("holder", "normal", "high")
            ]
            submit(holder, first, "normal", f"round-{number}-shared")
            until(lambda holder=holder: active(holder))
            submit(normal, second, "normal", f"round-{number}-shared")
            assert state["jobs"][holder]["job"]["job_id"] != state["jobs"][normal]["job"]["job_id"]
            until(lambda normal=normal: pending(normal))
            submit(high, first, "high", f"round-{number}-high")
            until(lambda high=high: pending(high))
            for label, requester in ((holder, second), (normal, first)):
                path = "/jobs/" + state["jobs"][label]["job"]["job_id"]
                api(requester, "GET", path, expected=404)
                api(requester, "POST", path + "/cancel", expected=404)
            low = state["jobs"][normal]["pending"]["workload"]
            urgent = state["jobs"][high]["pending"]["workload"]
            assert low["spec"]["priority"] == 100 and urgent["spec"]["priority"] == 1000

            def high_admitted(high=high):
                sample = snapshot(high)
                return (
                    sample
                    if sample and condition(sample["workload"], "Admitted").get("status") == "True"
                    else None
                )

            admitted = until(high_admitted)
            low_waiting = snapshot(normal)
            assert condition(low_waiting["workload"], "Admitted").get("status") != "True"
            state["rounds"].append(
                {
                    "high_project": first,
                    "older_normal_project": second,
                    "high_admission": admitted["workload"],
                    "normal_still_pending": low_waiting["workload"],
                }
            )
            save()
            for label in (holder, high, normal):
                assert until(lambda label=label: terminal(label))["state"] == "SUCCEEDED"
                snapshot(label)
            print("round", number, "priority order and three GPU results passed", flush=True)

        for project in clients:
            other_ids = {
                v["job"]["job_id"] for v in state["jobs"].values() if v["project"] != project
            }
            assert not other_ids.intersection(j["job_id"] for j in api(project, "GET", "/jobs"))
            assert not other_ids.intersection(
                x["body"]["job_id"] for x in api(project, "GET", "/usage")
            )
            overview = api(project, "GET", "/overview")
            assert overview["project_ref"] == config["projects"][project]["project_ref"]
            assert not other_ids.intersection(j["job_id"] for j in overview["jobs"]["items"])
            assert not other_ids.intersection(
                x["body"]["job_id"] for x in overview["history"]["items"]
            )
        state["checks"].update(
            project_lists_scoped=True,
            priority_order_both_projects=True,
            same_key_distinct_projects=True,
            retries_idempotent=True,
        )
        state["result"] = "PASS"
    finally:
        errors = []
        for label, entry in state["jobs"].items():
            try:
                if not terminal(label):
                    api(entry["project"], "POST", "/jobs/" + entry["job"]["job_id"] + "/cancel")
                    until(lambda label=label: terminal(label), seconds=90)
            except (httpx.HTTPError, AssertionError, TimeoutError) as exc:
                errors.append({"job_id": entry["job"]["job_id"], "error": type(exc).__name__})
        state["cleanup_errors"] = errors
        save()
        for client in clients.values():
            client.close()
        assert not errors, "inspect owned attempts; cleanup remains incomplete"
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = run(json.loads(args.config.read_text()), args.report)
    print(json.dumps({"result": result["result"], "checks": result["checks"]}))
