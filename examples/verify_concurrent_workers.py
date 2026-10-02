"""Run one API-owned GPU attempt with two already prepared traced test workers.

Requires a private config with api_url, ca_file, token, database_url, project,
namespace, worker_pods, normal_worker, cluster_queue, workload_ref, candidate_ref,
idempotency_key, mlflow_url and artifacts (S3Artifacts options). AWS credentials
come from the environment. This tool never scales services or retries submissions
under new keys. Its report is private and created exclusively before submission.
"""

import argparse
import hashlib
import json
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from resource_advisor.artifacts import S3Artifacts
from resource_advisor.contracts import TERMINAL
from resource_advisor.store import Store, outbox, usage


def run(config, path):
    with path.open("x"):
        pass
    path.chmod(0o600)
    report = {"status": "INCOMPLETE", "checks": {}, "snapshots": []}
    client = httpx.Client(
        base_url=config["api_url"] + "/api/v1/compute",
        verify=ssl.create_default_context(cafile=config["ca_file"]),
        headers={"Authorization": "Bearer " + config["token"]},
        timeout=20,
    )
    mlflow = httpx.Client(base_url=config["mlflow_url"], timeout=20)
    store = Store(config["database_url"])

    def save():
        path.write_text(json.dumps(report, indent=2) + "\n")

    def kube(*args):
        return subprocess.check_output(
            ["kubectl", "--request-timeout=15s", "-n", config["namespace"], *args],
            text=True,
            timeout=20,
        )

    def obj(kind, name):
        return json.loads(kube("get", kind, name, "-o", "json"))

    def api(method, route, **kwargs):
        response = client.request(method, route, **kwargs)
        response.raise_for_status()
        return response.json()

    def workers():
        values = [obj("pod", name) for name in config["worker_pods"]]
        assert len(values) == 2 and len({v["metadata"]["uid"] for v in values}) == 2
        for value in values:
            assert value["status"]["phase"] == "Running"
            assert all(
                c["ready"] and c["restartCount"] == 0 for c in value["status"]["containerStatuses"]
            )
        return values

    def until(fn, seconds=180):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = fn()
            if value:
                return value
            time.sleep(1)
        raise TimeoutError("inspect the saved attempt and same worker Pods; do not resubmit")

    try:
        assert obj("deployment", config["normal_worker"])["spec"]["replicas"] == 0
        assert all(j["state"] in TERMINAL for j in api("GET", "/jobs"))
        report["workers_before"] = workers()
        report["queue_before"] = obj("clusterqueue", config["cluster_queue"])
        request = {
            "workload_ref": config["workload_ref"],
            "candidate_ref": config["candidate_ref"],
            "mode": "observe",
        }
        headers = {"Idempotency-Key": config["idempotency_key"]}
        report["request"] = request
        save()
        job = api("POST", "/jobs", json=request, headers=headers)
        report["job"] = job
        save()
        repeated = api("POST", "/jobs", json=request, headers=headers)
        assert (job["job_id"], job["attempt_id"]) == (repeated["job_id"], repeated["attempt_id"])

        def terminal():
            current = api("GET", "/jobs/" + job["job_id"])
            report["snapshots"].append(current)
            if current["external_id"]:
                report["external_job"] = obj("job", current["external_id"])
                report["compute_pods"] = json.loads(
                    kube("get", "pods", "-l", "job-name=" + current["external_id"], "-o", "json")
                )["items"]
            save()
            return current if current["state"] in TERMINAL else None

        report["terminal"] = until(terminal)
        assert report["terminal"]["state"] == "SUCCEEDED"
        attempt = job["attempt_id"]

        def delivered():
            with store.transaction() as conn:
                events = list(
                    conn.execute(select(outbox).where(outbox.c.id.like("%" + attempt))).mappings()
                )
                receipt = store.get(conn, "artifact_tracking", attempt)
                if receipt and events and all(e["status"] == "DONE" for e in events):
                    report["events"] = [dict(e) for e in events]
                    return True
            return False

        until(delivered)
        report["workers_after"] = workers()
        assert [p["metadata"]["uid"] for p in report["workers_before"]] == [
            p["metadata"]["uid"] for p in report["workers_after"]
        ]
        traces = []
        for name in config["worker_pods"]:
            traces += [
                json.loads(line.removeprefix("RA_CONCURRENCY "))
                for line in kube("logs", name).splitlines()
                if line.startswith("RA_CONCURRENCY ")
            ]
        relevant = [t for t in traces if t.get("attempt_id") == attempt]
        assert sum(t["action"] == "submit_started" for t in relevant) == 1
        assert (
            len(
                {
                    t["worker"]
                    for t in relevant
                    if t["action"] == "observed" and t["state"] == "RUNNING"
                }
            )
            == 2
        )
        report["traces"] = traces
        assert len(report["compute_pods"]) == 1
        assert report["compute_pods"][0]["status"]["phase"] == "Succeeded"
        assert report["external_job"]["metadata"]["name"] == attempt
        workloads = json.loads(kube("get", "workloads", "-o", "json"))["items"]
        report["workloads"] = [
            w
            for w in workloads
            if any(
                o["uid"] == report["external_job"]["metadata"]["uid"]
                for o in w["metadata"].get("ownerReferences", [])
            )
        ]
        assert len(report["workloads"]) == 1
        with store.transaction() as conn:
            report["usage"] = dict(
                conn.execute(select(usage).where(usage.c.attempt_id == attempt)).mappings().one()
            )
            for kind, ref in [
                ("result", attempt),
                ("tracking", attempt),
                ("artifact", "artifact-" + attempt),
                ("artifact_tracking", attempt),
            ]:
                row = store.get(conn, kind, ref)
                assert row and row["project"] == config["project"]
                report[kind] = row["body"]
        assert report["usage"]["project"] == config["project"]
        assert report["result"]["evidence_kind"] == "hardware"
        link = report["tracking"]
        response = mlflow.post(
            "/api/2.0/mlflow/runs/search",
            json={
                "experiment_ids": [link["experiment_id"]],
                "filter": f"tags.`resource_advisor.attempt_id` = '{attempt}'",
                "max_results": 2,
            },
        )
        response.raise_for_status()
        runs = response.json().get("runs", [])
        assert len(runs) == 1 and runs[0]["info"]["run_id"] == link["run_id"]
        assert runs[0]["info"]["status"] == "FINISHED"
        assert {t["key"]: t["value"] for t in runs[0]["data"]["tags"]}["project"] == config[
            "project"
        ]
        artifact = report["artifact"]
        data = S3Artifacts(**config["artifacts"]).read(artifact)
        response = client.get("/artifacts/" + artifact["ref"] + "/content")
        response.raise_for_status()
        assert response.content == data
        uri = urlparse(runs[0]["info"]["artifact_uri"])
        assert uri.scheme == "mlflow-artifacts" and not uri.netloc
        response = mlflow.get(
            "/api/2.0/mlflow-artifacts/artifacts/"
            + uri.path.lstrip("/")
            + "/"
            + report["artifact_tracking"]["path"]
        )
        response.raise_for_status()
        assert response.content == data
        assert "sha256:" + hashlib.sha256(data).hexdigest() == artifact["digest"]
        report["queue_after"] = obj("clusterqueue", config["cluster_queue"])
        assert report["queue_before"]["spec"] == report["queue_after"]["spec"]
        assert not any(
            report["queue_after"]["status"].get(k, 0)
            for k in ["pendingWorkloads", "admittedWorkloads", "reservingWorkloads"]
        )
        report["checks"] = dict(
            two_workers_observed_running=True,
            sole_submit=True,
            sole_job_and_pod=True,
            idempotent_replay=True,
            sole_ledger_and_mlflow=True,
            artifact_bytes_match=True,
            quota_unchanged=True,
        )
        report["status"] = "PASS"
        print(json.dumps({"status": "PASS", "checks": report["checks"]}))
    finally:
        save()
        client.close()
        mlflow.close()
        store.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    run(json.loads(args.config.read_text()), args.report)
