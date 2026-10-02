"""Read every restored result through S3, project API routes and MLflow.

This uses the API in-process, not a deployed recovery HTTP server. It never starts
a worker or collector. Supply a restored database URL via RA_RESTORED_DATABASE_URL,
AWS credentials via environment, and private JSON config with credentials_file,
artifacts_file, mlflow_url and project_tokens. Keep config/report outside Git.
"""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from resource_advisor.api import create_app
from resource_advisor.artifacts import S3Artifacts
from resource_advisor.backup import fingerprint
from resource_advisor.configuration import api_configuration, read_config
from resource_advisor.service import Service
from resource_advisor.store import jobs, outbox, usage


def verify(store, config, report_path):
    with report_path.open("x"):
        pass
    report_path.chmod(0o600)
    artifact_config = read_config(Path(config["artifacts_file"]))
    identities = api_configuration(read_config(Path(config["credentials_file"])), artifact_config)
    storage = S3Artifacts(**artifact_config)
    tokens = config["project_tokens"]
    if len(tokens) < 2:
        raise ValueError("two project tokens required for foreign-owner negative checks")
    before = fingerprint(store)
    report = {"status": "INCOMPLETE", "artifacts": [], "tracking": []}
    started = time.monotonic()

    def save():
        report_path.write_text(json.dumps(report, indent=2) + "\n")

    save()
    try:
        with (
            TestClient(create_app(Service(store), identities, artifact_storage=storage)) as app,
            httpx.Client(base_url=config["mlflow_url"], timeout=20) as mlflow,
            store.transaction() as conn,
        ):
            all_jobs = {r["id"]: dict(r) for r in conn.execute(select(jobs)).mappings()}
            assert all_jobs, "a restore acceptance requires existing execution history"
            assert app.get("/api/v1/compute/jobs").status_code == 401
            for row in store.list(conn, "artifact"):
                artifact, project = row["body"], row["project"]
                job = all_jobs[artifact["job_id"]]
                attempt = job["body"]["attempt_id"]
                assert job["project"] == project == artifact["project_ref"]
                assert attempt == artifact["attempt_id"]
                assert row["ref"] == artifact["ref"] == "artifact-" + attempt
                result = store.get(conn, "result", attempt)
                assert result["project"] == project
                content = storage.read(artifact)
                payload = json.loads(content)
                assert payload["result"] == result["body"]
                assert payload["result_digest"] == job["body"]["result_digest"]
                path = "/api/v1/compute/artifacts/" + artifact["ref"] + "/content"
                response = app.get(path, headers={"Authorization": "Bearer " + tokens[project]})
                assert response.status_code == 200 and response.content == content
                for other, token in tokens.items():
                    if other != project:
                        assert (
                            app.get(path, headers={"Authorization": "Bearer " + token}).status_code
                            == 404
                        )
                receipt = store.get(conn, "artifact_tracking", attempt)
                assert receipt and receipt["project"] == project
                tracking = store.get(conn, "tracking", attempt)
                assert tracking["project"] == project
                assert receipt["body"]["run_id"] == tracking["body"]["run_id"]
                response = mlflow.get(
                    "/api/2.0/mlflow/runs/get", params={"run_id": tracking["body"]["run_id"]}
                )
                response.raise_for_status()
                run = response.json()["run"]
                assert run["info"]["run_id"] == tracking["body"]["run_id"]
                uri = urlparse(run["info"]["artifact_uri"])
                parts = uri.path.lstrip("/").split("/")
                assert uri.scheme == "mlflow-artifacts" and not (
                    uri.netloc or uri.query or uri.fragment
                )
                assert all(p and p.replace("-", "").isalnum() for p in parts)
                name = "resource-advisor/" + artifact["digest"][7:] + ".json"
                assert receipt["body"]["path"] == name
                response = mlflow.get(
                    "/api/2.0/mlflow-artifacts/artifacts/" + "/".join(parts) + "/" + name
                )
                response.raise_for_status()
                assert response.content == content
                report["artifacts"].append(
                    {
                        "attempt_id": attempt,
                        "project": project,
                        "digest": artifact["digest"],
                        "size_bytes": len(content),
                        "api_matches": True,
                        "mlflow_matches": True,
                        "foreign_http": 404,
                    }
                )
                if len(report["artifacts"]) % 50 == 0:
                    save()
                    print("artifacts verified", len(report["artifacts"]), flush=True)
            for row in store.list(conn, "tracking"):
                link = row["body"]
                job = all_jobs[link["job_id"]]
                attempt = job["body"]["attempt_id"]
                assert row["ref"] == attempt
                assert row["project"] == job["project"]
                response = mlflow.get("/api/2.0/mlflow/runs/get", params={"run_id": link["run_id"]})
                response.raise_for_status()
                run = response.json()["run"]
                assert run["info"]["run_id"] == link["run_id"]
                tags = {x["key"]: x["value"] for x in run["data"]["tags"]}
                assert run["info"]["experiment_id"] == link["experiment_id"]
                assert tags["project"] == row["project"]
                assert tags["resource_advisor.attempt_id"] == attempt
                ledger = (
                    conn.execute(select(usage).where(usage.c.attempt_id == attempt))
                    .mappings()
                    .all()
                )
                assert len(ledger) == 1 and ledger[0]["project"] == job["project"]
                report["tracking"].append(
                    {
                        "attempt_id": attempt,
                        "project": row["project"],
                        "run_id": link["run_id"],
                        "status": run["info"]["status"],
                        "ledger_rows": 1,
                    }
                )
            assert len(report["tracking"]) == len(all_jobs)
            assert {r["attempt_id"] for r in report["tracking"]} == {
                j["body"]["attempt_id"] for j in all_jobs.values()
            }
            expected_artifacts = {
                j["body"]["attempt_id"]
                for j in all_jobs.values()
                if j["state"] in {"SUCCEEDED", "FAILED"} and j["body"].get("result_digest")
            }
            assert expected_artifacts and len(report["artifacts"]) == len(expected_artifacts)
            assert {r["attempt_id"] for r in report["artifacts"]} == expected_artifacts
            assert not conn.execute(select(outbox.c.id).where(outbox.c.status != "DONE")).all()
        assert before == fingerprint(store)
        report.update(
            status="PASS",
            table_contents_unchanged=True,
            job_count=len(all_jobs),
            artifact_count=len(report["artifacts"]),
            tracking_count=len(report["tracking"]),
            elapsed_seconds=time.monotonic() - started,
        )
    finally:
        save()
    return report


if __name__ == "__main__":
    from resource_advisor.store import Store

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    restored = Store(os.environ["RA_RESTORED_DATABASE_URL"])
    try:
        result = verify(restored, read_config(args.config), args.report)
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "status",
                        "job_count",
                        "artifact_count",
                        "tracking_count",
                        "table_contents_unchanged",
                    )
                }
            )
        )
    finally:
        restored.engine.dispose()
