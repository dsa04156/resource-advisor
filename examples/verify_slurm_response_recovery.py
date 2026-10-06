"""One accepted-response/SIGKILL trial on an existing, dedicated Slurm lab worker.

Private config: lab_only, api_url, ca_file, token, database_url, project, cluster,
namespace, deployment, container, workload_ref, scheduling_profile_ref,
idempotency_key, mlflow_url and artifacts (S3Artifacts options). AWS credentials
come from the environment. The exclusive report directory contains private
infrastructure evidence; never commit it. No scheduler policy/lease is changed.
"""

import argparse
import hashlib
import json
import ssl
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from resource_advisor.artifacts import S3Artifacts
from resource_advisor.contracts import TERMINAL, ExecutionResult, signature
from resource_advisor.store import Store, jobs, outbox, usage

CRASH_CODE = """import json,os,signal
from pathlib import Path
matches=[]
for path in Path('/proc').glob('[0-9]*/cmdline'):
 try: argv=path.read_bytes().split(bytes([0]))
 except OSError: continue
 if argv[1:4]==[b'-m',b'resource_advisor.cli',b'worker']:
  matches.append(int(path.parent.name))
assert len(matches)==1 and matches[0]>1,matches
with open('/tmp/worker-crash-request.json','x') as f:
 json.dump({'worker_pid':matches[0]},f);f.flush();os.fsync(f.fileno())
print(json.dumps({'worker_pid':matches[0]}),flush=True)
os.kill(matches[0],signal.SIGKILL)
"""

REMOTE_FILES_CODE = """import json
from pathlib import Path
names=['accepted-slurm-submit.json','slurm-submit-attempts.jsonl','worker-crash-request.json']
print(json.dumps({name:Path('/tmp',name).read_text() for name in names if Path('/tmp',name).exists()}))
"""


def capture_attempt(store, job, project):
    """Canonical refs, including records whose bodies contain only job/run IDs."""
    attempt = job["attempt_id"]
    with store.transaction() as conn:
        row = dict(conn.execute(select(jobs).where(jobs.c.id == job["job_id"])).mappings().one())
        assert row["project"] == project
        records = {}
        for kind, ref in (
            ("result", attempt),
            ("tracking", attempt),
            ("artifact", "artifact-" + attempt),
            ("artifact_tracking", attempt),
        ):
            record = store.get(conn, kind, ref)
            if record:
                assert record["project"] == project
                records[kind] = dict(record)
        ledger = [
            dict(r)
            for r in conn.execute(select(usage).where(usage.c.attempt_id == attempt)).mappings()
        ]
        events = [
            dict(r)
            for r in conn.execute(select(outbox)).mappings()
            if r["body"].get("attempt_id") == attempt
            or r["body"].get("job_id") == job["job_id"]
            or r["body"].get("artifact_ref") == "artifact-" + attempt
        ]
    return {"db_job": row, "records": records, "ledger": ledger, "outbox": events}


def verify_files(files, attempt):
    receipt = json.loads(files["accepted-slurm-submit.json"])
    calls = [json.loads(line) for line in files["slurm-submit-attempts.jsonl"].splitlines()]
    assert receipt["attempt"] == attempt
    assert len(calls) == 1 and calls[0]["attempt"] == attempt
    assert json.loads(files["worker-crash-request.json"])["worker_pid"] > 1
    return receipt


def run(config, directory):
    assert config["lab_only"] is True, "dedicated lab deployment required"
    directory.mkdir(mode=0o700)
    client = httpx.Client(
        base_url=config["api_url"] + "/api/v1/compute",
        verify=ssl.create_default_context(cafile=config["ca_file"]),
        headers={"Authorization": "Bearer " + config["token"]},
        timeout=20,
    )
    store = Store(config["database_url"])
    report = {"status": "INCOMPLETE", "checks": {}, "snapshots": []}
    before = None
    changed = False
    pod_name = None
    fault = "ra-submit-fault-" + uuid.uuid4().hex[:10]
    command_number = 0

    def save(name, value):
        temporary = directory / (name + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
        temporary.chmod(0o600)
        temporary.replace(directory / name)

    def checkpoint():
        save("report.json", report)

    def kube(*args, data=None, check=True):
        nonlocal command_number
        command_number += 1
        result = subprocess.run(
            ["rtk", "proxy", "kubectl", "--request-timeout=15s", "-n", config["namespace"], *args],
            input=data,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            save(
                f"command-{command_number}-failure.json",
                {
                    "argv": args,
                    "exit": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
            )
            if check:
                raise RuntimeError(
                    f"kubectl failed; private command-{command_number} evidence retained"
                )
        return result

    def obj(kind, name):
        return json.loads(kube("get", kind, name, "-o", "json").stdout)

    def api(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def until(fn, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            result = fn()
            if result:
                return result
            time.sleep(1)
        raise TimeoutError("inspect the same saved attempt; do not submit under another key")

    def pods():
        selector = ",".join(
            f"{k}={v}" for k, v in before["spec"]["selector"]["matchLabels"].items()
        )
        return json.loads(kube("get", "pods", "-l", selector, "-o", "json").stdout)["items"]

    def files():
        response = kube(
            "exec",
            pod_name,
            "-c",
            config["container"],
            "--",
            "python",
            "-c",
            REMOTE_FILES_CODE,
            check=False,
        )
        return json.loads(response.stdout) if response.returncode == 0 else None

    def patch(template, replicas):
        data = [
            {"op": "replace", "path": "/spec/template", "value": template},
            {"op": "replace", "path": "/spec/replicas", "value": replicas},
        ]
        save("patch.json", data)
        kube(
            "patch",
            "deployment",
            config["deployment"],
            "--type=json",
            "--patch-file",
            str(directory / "patch.json"),
        )

    try:
        before = obj("deployment", config["deployment"])
        save("deployment-before.json", before)
        assert before["spec"]["replicas"] == before["status"].get("readyReplicas") == 1
        assert len(before["spec"]["template"]["spec"]["containers"]) == 1
        current = pods()
        assert len(current) == 1
        route_code = "from pathlib import Path;print(Path('/config/worker.json').read_text())"
        routes = json.loads(
            kube(
                "exec",
                current[0]["metadata"]["name"],
                "-c",
                config["container"],
                "--",
                "python",
                "-c",
                route_code,
            ).stdout
        )["routes"]
        assert len(routes) == 1
        assert (routes[0]["project"], routes[0]["cluster"], routes[0]["backend"]) == (
            config["project"],
            config["cluster"],
            "slurm",
        )
        overview = api("GET", "/overview")
        assert overview["project_ref"] == config["project"]
        assert all(
            state in TERMINAL or count == 0 for state, count in overview["job_counts"].items()
        )
        with store.transaction() as conn:
            assert not conn.execute(select(outbox.c.id).where(outbox.c.status != "DONE")).first()
        queue_code = """import json
from pathlib import Path
from resource_advisor.backends import SlurmBackend
b=SlurmBackend(**json.loads(Path('/config/worker.json').read_text())['routes'][0]['options'])
print(b.call(['squeue','--noheader','--account',b.account,'--partition',b.partition,'--format=%i|%j|%a|%P|%T']))
"""
        assert not kube(
            "exec",
            current[0]["metadata"]["name"],
            "-c",
            config["container"],
            "--",
            "python",
            "-c",
            queue_code,
        ).stdout.strip()
        changed = True
        kube("scale", "deployment/" + config["deployment"], "--replicas=0")
        until(lambda: not pods(), 60)
        body = {
            "workload_ref": config["workload_ref"],
            "scheduling_profile_ref": config["scheduling_profile_ref"],
            "mode": "observe",
        }
        report["request"] = {"body": body, "idempotency_key": config["idempotency_key"]}
        checkpoint()
        job = api(
            "POST", "/jobs", json=body, headers={"Idempotency-Key": config["idempotency_key"]}
        )
        report["job"] = job
        checkpoint()
        assert job["state"] == "VALIDATED"
        assert (
            capture_attempt(store, job, config["project"])["db_job"]["body"]["backend_cluster_id"]
            == config["cluster"]
        )
        cm = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": fault},
            "data": {"ssh": Path(__file__).with_name("hold_slurm_submit_response.py").read_text()},
        }
        kube("create", "-f", "-", data=json.dumps(cm))
        template = json.loads(json.dumps(before["spec"]["template"]))
        spec = template["spec"]
        spec["shareProcessNamespace"] = True
        spec["volumes"].append(
            {"name": "submit-fault", "configMap": {"name": fault, "defaultMode": 493}}
        )
        container = spec["containers"][0]
        assert container["name"] == config["container"]
        container["volumeMounts"].append(
            {"name": "submit-fault", "mountPath": "/fault", "readOnly": True}
        )
        assert not any(
            e["name"] in {"PATH", "RA_RECOVERY_ATTEMPT"} for e in container.get("env", [])
        )
        container.setdefault("env", []).extend(
            [
                {
                    "name": "PATH",
                    "value": "/fault:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
                },
                {"name": "RA_RECOVERY_ATTEMPT", "value": job["attempt_id"]},
            ]
        )
        patch(template, 1)

        def accepted():
            nonlocal pod_name
            current = pods()
            if len(current) != 1:
                return None
            pod_name = current[0]["metadata"]["name"]
            captured = files()
            if not captured or "accepted-slurm-submit.json" not in captured:
                return None
            save("files-at-acceptance.json", captured)
            observation = capture_attempt(store, job, config["project"])
            report["boundary"] = {"pod": current[0], "files": captured, "observation": observation}
            checkpoint()
            return json.loads(captured["accepted-slurm-submit.json"])

        receipt = until(accepted, 90)
        assert receipt["attempt"] == job["attempt_id"]
        assert (
            datetime.now(UTC) - datetime.fromisoformat(receipt["accepted_at"])
        ).total_seconds() < 15
        boundary = report["boundary"]["observation"]["db_job"]
        assert boundary["state"] == "SUBMITTING" and not boundary["body"].get("external_id")
        killed = kube(
            "exec",
            pod_name,
            "-c",
            config["container"],
            "--",
            "python",
            "-c",
            CRASH_CODE,
            check=False,
        )
        save(
            "crash-exec.json",
            {"exit": killed.returncode, "stdout": killed.stdout, "stderr": killed.stderr},
        )

        def restarted():
            current = obj("pod", pod_name)
            states = current.get("status", {}).get("containerStatuses", [])
            if states and states[0].get("restartCount", 0) >= 1:
                report["restart"] = current
                checkpoint()
                assert current["metadata"]["uid"] == report["boundary"]["pod"]["metadata"]["uid"]
                assert states[0]["lastState"]["terminated"]["exitCode"] == 137
                assert states[0]["lastState"]["terminated"].get("reason") != "OOMKilled"
                return True
            return False

        until(restarted, 60)

        def completed():
            captured = files()
            if captured:
                save("files-after-restart.json", captured)
                report["files_after_restart"] = captured
            observation = capture_attempt(store, job, config["project"])
            report["snapshots"].append(observation)
            terminal = api("GET", "/jobs/" + job["job_id"])
            report["terminal"] = terminal
            checkpoint()
            return (
                observation
                if terminal["state"] in TERMINAL
                and "artifact_tracking" in observation["records"]
                and all(e["status"] == "DONE" for e in observation["outbox"])
                else None
            )

        observation = until(completed, 600)
        # Persist ephemeral evidence BEFORE any post-run assertion or cleanup.
        captured = files()
        save("files-at-terminal.json", captured)
        assert verify_files(captured, job["attempt_id"]) == receipt
        assert report["terminal"]["state"] == "SUCCEEDED"
        assert report["terminal"]["external_id"] == receipt["external_id"]
        assert (
            len(observation["ledger"]) == 1
            and observation["ledger"][0]["project"] == config["project"]
        )
        native_code = (
            queue_code.split("print(b.call")[0]
            + f"""
a=b.call(['sacct','--noheader','--parsable2','--name',{job["attempt_id"]!r},'--accounts',b.account,'--partition',b.partition,'--starttime',{job["created_at"][:19]!r},'--format=JobIDRaw,JobName%100,Account%100,Partition%100'])
parents=[line.split('|')[0] for line in a.splitlines() if line.split('|')[0].isdigit()]
q=b.call(['squeue','--noheader','--account',b.account,'--partition',b.partition,'--format=%i|%j|%a|%P|%T'])
print(json.dumps({{'parents':parents,'queue':q,'accounting':a}}))
"""
        )
        native = json.loads(
            kube(
                "exec", pod_name, "-c", config["container"], "--", "python", "-c", native_code
            ).stdout
        )
        report["native"] = native
        checkpoint()
        assert native["parents"] == [receipt["external_id"]] and not native["queue"].strip()
        records = {k: v["body"] for k, v in observation["records"].items()}
        assert (
            signature(ExecutionResult.model_validate(records["result"]))
            == report["terminal"]["result_digest"]
        )
        data = S3Artifacts(**config["artifacts"]).read(records["artifact"])
        response = client.get("/artifacts/" + records["artifact"]["ref"] + "/content")
        response.raise_for_status()
        assert response.content == data
        bundle = json.loads(data)
        assert (
            bundle["result"] == records["result"]
            and bundle["result_digest"] == report["terminal"]["result_digest"]
        )
        with httpx.Client(base_url=config["mlflow_url"], timeout=20) as mlflow:
            tracking = records["tracking"]
            response = mlflow.post(
                "/api/2.0/mlflow/runs/search",
                json={
                    "experiment_ids": [tracking["experiment_id"]],
                    "filter": f"tags.`resource_advisor.attempt_id` = '{job['attempt_id']}'",
                    "max_results": 2,
                },
            )
            response.raise_for_status()
            runs = response.json().get("runs", [])
            report["mlflow"] = runs
            checkpoint()
            assert len(runs) == 1 and runs[0]["info"]["run_id"] == tracking["run_id"]
            assert runs[0]["info"]["status"] == "FINISHED"
            assert {t["key"]: t["value"] for t in runs[0]["data"]["tags"]}["project"] == config[
                "project"
            ]
            uri = urlparse(runs[0]["info"]["artifact_uri"])
            assert uri.scheme == "mlflow-artifacts" and not uri.netloc
            response = mlflow.get(
                "/api/2.0/mlflow-artifacts/artifacts/"
                + uri.path.lstrip("/")
                + "/"
                + records["artifact_tracking"]["path"]
            )
            response.raise_for_status()
            assert response.content == data
        replay = api(
            "POST", "/jobs", json=body, headers={"Idempotency-Key": config["idempotency_key"]}
        )
        assert replay["job_id"] == job["job_id"]
        report["checks"] = {
            "accepted_without_ack": True,
            "same_pod_exit137_restart": True,
            "one_submit_invocation": True,
            "same_native_parent": True,
            "one_ledger_and_mlflow": True,
            "s3_api_mlflow_bytes_match": True,
            "idempotent_replay": True,
            "native_queue_empty": True,
        }
        report["bundle_sha256"] = hashlib.sha256(data).hexdigest()
        checkpoint()
    finally:
        # Recover as much evidence as possible even when an assertion failed.
        try:
            if changed:
                if pod_name:
                    try:
                        captured = files()
                        if captured:
                            save("files-before-restore.json", captured)
                    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                        report["cleanup_capture_error"] = type(exc).__name__
                patch(before["spec"]["template"], before["spec"]["replicas"])
                kube("rollout", "status", "deployment/" + config["deployment"], "--timeout=60s")
                kube("delete", "configmap", fault, "--ignore-not-found")
                restored = obj("deployment", config["deployment"])
                save("deployment-restored.json", restored)
                assert restored["spec"]["template"] == before["spec"]["template"]
                assert restored["status"].get("readyReplicas") == before["spec"]["replicas"]
                report["restored"] = True
            if report["checks"] and report.get("restored"):
                report["status"] = "PASS"
        finally:
            checkpoint()
            client.close()
            store.engine.dispose()
    print(json.dumps({"status": report["status"], "checks": report["checks"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    run(json.loads(args.config.read_text()), args.directory.resolve())
