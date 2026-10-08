"""One live API Job while the dedicated lab MLflow server is stopped.

Requires private operator configuration and the prospectively frozen protocol
in docs/reference/mlflow-server-outage-v2.md. No replacement computation on failure.
"""

import argparse
import json
import ssl
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from resource_advisor.artifacts import S3Artifacts
from resource_advisor.contracts import TERMINAL, ExecutionResult, signature
from resource_advisor.inventory import quantity
from resource_advisor.store import Store, entities, jobs, outbox, usage


def assert_gpu_requests(requests):
    """Compare quantities, since the API canonicalizes 2048Mi to 2Gi."""
    expected = {"cpu": "1", "memory": "2048Mi", "nvidia.com/gpu": "1"}
    assert set(requests) == set(expected), "unexpected resource request keys"
    assert all(quantity(requests[key]) == quantity(value) for key, value in expected.items()), (
        "allocation differs from the frozen one-CPU/2GiB/one-GPU request"
    )


def run(config, directory):
    import boto3
    from botocore.config import Config

    assert config["lab_only"] is True and config["maximum_outage_seconds"] <= 90
    directory.mkdir(mode=0o700)
    state = {"status": "INCOMPLETE", "phase": "prerequisites"}
    store = Store(config["database_url"])
    credentials = json.loads(Path(config["artifact_credentials_file"]).read_text())
    artifact_client = boto3.client(
        "s3",
        endpoint_url=config["artifact_storage"]["endpoint_url"],
        aws_access_key_id=credentials["access_key"],
        aws_secret_access_key=credentials["secret_key"],
        config=Config(connect_timeout=5, read_timeout=15, s3={"addressing_style": "path"}),
    )
    storage = S3Artifacts(client=artifact_client, **config["artifact_storage"])
    tls = ssl.create_default_context(cafile=config["tls_ca"])
    api = httpx.Client(
        base_url=config["api_url"],
        verify=tls,
        timeout=10,
        headers={"Authorization": "Bearer " + config["user_token"]},
    )
    operator = httpx.Client(
        base_url=config["api_url"],
        verify=tls,
        timeout=10,
        headers={"Authorization": "Bearer " + config["operator_token"]},
    )
    forward = None
    restore_needed = False

    def save(name, value):
        path = directory / name
        path.write_text(json.dumps(value, indent=2) + "\n")
        path.chmod(0o600)

    def checkpoint():
        save("report.json", state)

    def kube(namespace, *args):
        r = subprocess.run(
            ["rtk", "proxy", "kubectl", "--request-timeout=10s", "-n", namespace, *args],
            text=True,
            capture_output=True,
            timeout=25,
        )
        if r.returncode:
            save("native-error.json", {"args": args, "exit": r.returncode, "stderr": r.stderr})
            raise RuntimeError("native operation failed; preserve original accepted IDs")
        return r.stdout

    def get(namespace, kind, name=None):
        return json.loads(kube(namespace, "get", kind, *([name] if name else []), "-o", "json"))

    def native():
        return {
            kind: {
                r["metadata"]["uid"]: signature(r["spec"])
                for r in get(config["namespace"], kind)["items"]
            }
            for kind in (
                "jobs",
                "pods",
                "deployments",
                "persistentvolumeclaims",
                "clusterqueues.kueue.x-k8s.io",
                "workloads.kueue.x-k8s.io",
            )
        }

    def immutable():
        with store.transaction() as conn:
            records = [
                dict(r)
                for r in conn.execute(
                    select(entities).where(
                        entities.c.kind.in_(
                            [
                                "result",
                                "profile",
                                "tracking",
                                "artifact",
                                "artifact_tracking",
                                "capability",
                                "variant",
                                "workload",
                            ]
                        )
                    )
                ).mappings()
            ]
            ledgers = [dict(r) for r in conn.execute(select(usage)).mappings()]
            terminal = [
                dict(r)
                for r in conn.execute(
                    select(jobs).where(jobs.c.state.in_([s.value for s in TERMINAL]))
                ).mappings()
            ]
        return {
            "entities": {r["kind"] + "/" + r["ref"]: signature(r) for r in records},
            "ledgers": {r["attempt_id"]: signature(r) for r in ledgers},
            "terminal_jobs": {r["id"]: signature(r) for r in terminal},
        }

    def mlflow_snapshot(base):
        with httpx.Client(base_url=base, timeout=10) as client:
            r = client.get("/api/2.0/mlflow/experiments/search", params={"max_results": 1000})
            r.raise_for_status()
            assert not r.json().get("next_page_token")
            ids = [e["experiment_id"] for e in r.json()["experiments"]]
            r = client.post(
                "/api/2.0/mlflow/runs/search", json={"experiment_ids": ids, "max_results": 1000}
            )
            r.raise_for_status()
            assert not r.json().get("next_page_token")
            return {run["info"]["run_id"]: run for run in r.json().get("runs", [])}

    def target():
        with store.transaction() as conn:
            row = dict(store.job(conn, state["job_id"]))
            attempt = row["body"]["attempt_id"]
            ledger = [
                dict(r)
                for r in conn.execute(select(usage).where(usage.c.attempt_id == attempt)).mappings()
            ]
            event = (
                dict(
                    conn.execute(select(outbox).where(outbox.c.id == "mlflow-" + attempt))
                    .mappings()
                    .one()
                )
                if row["state"] in TERMINAL
                else None
            )
            found = {}
            for kind in ("result", "profile", "tracking", "artifact", "artifact_tracking"):
                record = store.get(
                    conn, kind, attempt if kind != "artifact" else "artifact-" + attempt
                )
                found[kind] = dict(record) if record is not None else None
        return {"job": row, "ledger": ledger, "outbox": event, **found}

    def restore():
        nonlocal restore_needed
        if restore_needed:
            kube(
                config["mlflow_namespace"],
                "scale",
                "deployment/" + config["mlflow_deployment"],
                "--replicas=" + str(state["original_replicas"]),
            )
            state["restore_requested_at"] = datetime.now().astimezone().isoformat()
            restore_needed = False
            checkpoint()

    checkpoint()
    try:
        namespace = config["mlflow_namespace"]
        deployment = get(namespace, "deployment", config["mlflow_deployment"])
        service = get(namespace, "service", config["mlflow_service"])
        pvc_name = next(
            v["persistentVolumeClaim"]["claimName"]
            for v in deployment["spec"]["template"]["spec"]["volumes"]
            if "persistentVolumeClaim" in v
        )
        pvc = get(namespace, "pvc", pvc_name)
        assert deployment["spec"]["replicas"] == deployment["status"]["readyReplicas"] == 1
        assert pvc["status"]["phase"] == "Bound"
        state.update(
            original_replicas=1,
            mlflow_before={"deployment": deployment, "service": service, "pvc": pvc},
            immutable_before=immutable(),
            native_before=native(),
            runs_before=mlflow_snapshot(config["mlflow_read_url"]),
        )
        assert all(r["info"]["status"] != "RUNNING" for r in state["runs_before"].values())
        with store.transaction() as conn:
            pending = list(
                conn.execute(
                    select(outbox).where(
                        outbox.c.kind.in_(["mlflow", "mlflow_artifact"]), outbox.c.status != "DONE"
                    )
                ).mappings()
            )
            active = list(
                conn.execute(
                    select(jobs).where(jobs.c.state.not_in([s.value for s in TERMINAL]))
                ).mappings()
            )
        assert not pending
        assert all(
            j["state"] == "CANCEL_REQUESTED"
            and j["body"]["candidate"]["backend"] == "slurm"
            and j["body"].get("external_id") == "61"
            for j in active
        )
        assert all(
            any(
                c["type"] in {"Complete", "Failed"} and c["status"] == "True"
                for c in j.get("status", {}).get("conditions", [])
            )
            for j in get(config["namespace"], "jobs")["items"]
        )
        checkpoint()
        for kind in ("capability", "variant", "workload"):
            value = json.loads(Path(config["private_state_directory"], kind + ".json").read_text())
            response = operator.post(
                "/api/v1/compute/"
                + {"capability": "capabilities", "variant": "variants", "workload": "workloads"}[
                    kind
                ],
                json=value,
            )
            response.raise_for_status()
            state[kind + "_registration"] = response.json()
            checkpoint()
        # Set the restoration obligation BEFORE dispatch, including a lost reply.
        restore_needed = True
        state.update(
            phase="stopping_server", outage_started_at=datetime.now().astimezone().isoformat()
        )
        checkpoint()
        outage_started = time.monotonic()
        kube(namespace, "scale", "deployment/" + config["mlflow_deployment"], "--replicas=0")
        while time.monotonic() - outage_started < 30:
            pods = json.loads(kube(namespace, "get", "pods", "-l", "app=mlflow", "-o", "json"))[
                "items"
            ]
            if not pods:
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("server did not stop; restore without compute")
        slices = json.loads(
            kube(
                namespace,
                "get",
                "endpointslices",
                "-l",
                "kubernetes.io/service-name=" + config["mlflow_service"],
                "-o",
                "json",
            )
        )
        state["server_offline"] = {"serving_pods": len(pods), "endpointslices": slices}
        checkpoint()
        assert not any(
            e.get("conditions", {}).get("ready")
            for s in slices["items"]
            for e in (s.get("endpoints") or [])
        )
        state["server_offline"] = {"serving_pods": len(pods), "endpointslices": slices}
        checkpoint()
        request = {
            "workload_ref": config["workload_ref"],
            "candidate_ref": config["candidate_ref"],
            "mode": "observe",
        }
        state.update(
            phase="single_api_submission", request=request, idempotency_key=config["run_key"]
        )
        checkpoint()
        response = api.post(
            "/api/v1/compute/jobs", json=request, headers={"Idempotency-Key": config["run_key"]}
        )
        response.raise_for_status()
        state.update(response.json())
        checkpoint()
        print(
            "One API Job accepted; observing same ID while actual MLflow server is stopped",
            flush=True,
        )
        while time.monotonic() - outage_started < config["maximum_outage_seconds"] - 30:
            r = api.get("/api/v1/compute/jobs/" + state["job_id"])
            r.raise_for_status()
            state["api_job_during_outage"] = r.json()
            observed = target()
            state["latest_target"] = observed
            checkpoint()
            if observed["job"]["state"] in TERMINAL:
                assert (
                    observed["job"]["state"] == "SUCCEEDED"
                    and observed["job"]["body"]["quality_passed"]
                )
                assert len(observed["ledger"]) == 1 and observed["result"] and observed["profile"]
                assert not observed["tracking"] and not observed["artifact_tracking"]
                event = observed["outbox"]
                if event["status"] == "PENDING" and event["last_error"] and event["tries"] >= 1:
                    state["outage_target"] = observed
                    state["outage_observed_seconds"] = time.monotonic() - outage_started
                    checkpoint()
                    break
            time.sleep(0.5)
        else:
            raise TimeoutError("bounded outage ended; restore and inspect SAME Job")
        restore()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = get(namespace, "deployment", config["mlflow_deployment"])
            if current["status"].get("readyReplicas") == 1:
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("original server restoration not Ready; no compute replay")
        log = (directory / "recovery-forward.log").open("w")
        forward = subprocess.Popen(
            [
                "rtk",
                "proxy",
                "kubectl",
                "--request-timeout=10s",
                "-n",
                namespace,
                "port-forward",
                "service/" + config["mlflow_service"],
                str(config["mlflow_recovery_local_port"]) + ":5000",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()
        base = "http://127.0.0.1:" + str(config["mlflow_recovery_local_port"])
        with httpx.Client(base_url=base, timeout=5) as client:
            for _ in range(40):
                if forward.poll() is not None:
                    raise RuntimeError("recovery forward exited; preserve Job and server")
                try:
                    r = client.get("/version")
                    r.raise_for_status()
                    assert r.text.strip() == "3.16.1"
                    break
                except httpx.HTTPError:
                    time.sleep(0.25)
            else:
                raise TimeoutError("restored server did not answer")
        state.update(
            phase="original_delivery_recovery",
            server_answered_at=datetime.now().astimezone().isoformat(),
        )
        checkpoint()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            observed = target()
            state["latest_target"] = observed
            checkpoint()
            if (
                observed["outbox"]["status"] == "DONE"
                and observed["tracking"]
                and observed["artifact"]
                and observed["artifact_tracking"]
            ):
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("same delivery still pending; never rerun compute")
        assert observed["job"] == state["outage_target"]["job"]
        assert observed["ledger"] == state["outage_target"]["ledger"]
        assert observed["result"] == state["outage_target"]["result"]
        assert observed["profile"] == state["outage_target"]["profile"]
        result = ExecutionResult.model_validate(observed["result"]["body"])
        body = observed["job"]["body"]
        assert signature(result) == body["result_digest"] and result.evidence_kind == "hardware"
        assert result.outcome == "COMPLETED" and result.measurements.quality_value == 1
        assert result.job_id == state["job_id"] and result.attempt_id == state["attempt_id"]
        assert (
            result.workload_signature == body["workload_signature"]
            and result.context_signature == body["context_signature"]
        )
        run_id = observed["tracking"]["body"]["run_id"]
        with httpx.Client(base_url=base, timeout=10) as client:
            r = client.post(
                "/api/2.0/mlflow/runs/search",
                json={
                    "experiment_ids": [config["experiment_id"]],
                    "filter": "tags.`resource_advisor.attempt_id` = '" + state["attempt_id"] + "'",
                    "max_results": 10,
                },
            )
            r.raise_for_status()
            runs = r.json().get("runs", [])
            assert len(runs) == 1
            run = runs[0]
            assert run["info"]["run_id"] == run_id and run["info"]["status"] == "FINISHED"
            tags = {t["key"]: t["value"] for t in run["data"]["tags"]}
            assert (
                tags["project"] == config["project_ref"]
                and tags["resource_advisor.job_id"] == state["job_id"]
            )
            assert tags["resource_advisor.attempt_id"] == state["attempt_id"]
            metrics = {m["key"]: m["value"] for m in run["data"]["metrics"]}
            for key, value in result.measurements.model_dump(mode="json").items():
                if value is not None:
                    assert metrics[key] == value
            assert (
                tags["workload_signature"] == body["workload_signature"]
                and tags["context_signature"] == body["context_signature"]
            )
            expected_end = int(datetime.fromisoformat(body["finished_at"]).timestamp() * 1000)
            assert run["info"]["end_time"] == expected_end
            uri = urlparse(run["info"]["artifact_uri"])
            assert uri.scheme == "mlflow-artifacts" and not uri.netloc
            r = client.get(
                "/api/2.0/mlflow-artifacts/artifacts/"
                + uri.path.strip("/")
                + "/"
                + observed["artifact_tracking"]["body"]["path"]
            )
            r.raise_for_status()
            mlflow_data = r.content
        r = api.get("/api/v1/compute/artifacts/" + observed["artifact"]["ref"] + "/content")
        r.raise_for_status()
        assert r.content == mlflow_data == storage.read(observed["artifact"]["body"])
        bundle = json.loads(r.content)
        assert bundle["result"] == result.model_dump(mode="json") and bundle[
            "result_digest"
        ] == signature(result)
        state.update(
            mlflow_run=run,
            matching_api_s3_mlflow_artifact_bytes=True,
            artifact_sha256=signature(bundle),
        )
        repeat = api.post(
            "/api/v1/compute/jobs", json=request, headers={"Idempotency-Key": config["run_key"]}
        )
        repeat.raise_for_status()
        assert (
            repeat.json()["job_id"] == state["job_id"]
            and repeat.json()["attempt_id"] == state["attempt_id"]
        )
        job = get(config["namespace"], "job", body["external_id"])
        pods = json.loads(
            kube(
                config["namespace"],
                "get",
                "pods",
                "-l",
                "job-name=" + body["external_id"],
                "-o",
                "json",
            )
        )["items"]
        assert len(pods) == 1 and pods[0]["status"]["phase"] == "Succeeded"
        assert pods[0]["status"]["containerStatuses"][0]["restartCount"] == 0
        assert_gpu_requests(pods[0]["spec"]["containers"][0]["resources"]["requests"])
        state.update(
            native_job=job,
            native_pod=pods[0],
            same_key_same_attempt=True,
            immutable_after=immutable(),
            native_after=native(),
            runs_after=mlflow_snapshot(base),
            mlflow_after={
                "deployment": get(namespace, "deployment", config["mlflow_deployment"]),
                "service": get(namespace, "service", config["mlflow_service"]),
                "pvc": get(namespace, "pvc", pvc_name),
            },
        )
        checkpoint()
        assert all(
            state["runs_after"].get(key) == value for key, value in state["runs_before"].items()
        )
        assert set(state["runs_after"]) - set(state["runs_before"]) == {run_id}
        for group, records in state["immutable_before"].items():
            assert all(
                state["immutable_after"][group].get(key) == digest
                for key, digest in records.items()
            ), group
        for kind, records in state["native_before"].items():
            assert all(
                state["native_after"][kind].get(uid) == digest for uid, digest in records.items()
            ), kind
        assert len(set(state["native_after"]["jobs"]) - set(state["native_before"]["jobs"])) == 1
        assert len(set(state["native_after"]["pods"]) - set(state["native_before"]["pods"])) == 1
        for kind in ("deployment", "service", "pvc"):
            assert (
                state["mlflow_before"][kind]["metadata"]["uid"]
                == state["mlflow_after"][kind]["metadata"]["uid"]
            )
            assert state["mlflow_before"][kind]["spec"] == state["mlflow_after"][kind]["spec"]
        assert observed["outbox"]["tries"] >= 2
        state.update(status="PASS", phase="completed")
        checkpoint()
        print("PASS: one original GPU Job/result/ledger and one recovered MLflow run", flush=True)
        return state
    finally:
        restore()
        if forward is not None:
            forward.terminate()
            forward.wait(timeout=15)
        api.close()
        operator.close()
        artifact_client.close()
        store.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    run(json.loads(args.config.read_text()), args.directory)
