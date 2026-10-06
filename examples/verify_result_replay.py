"""Verify retained terminal hardware attempts without submitting new computation.

Private config: api_url, ca_file, database_url, mlflow_url, project,
operator_token, user_token, foreign_operator_token, success_job_id,
canceled_job_id. Report contains private identifiers; never commit it.
See docs/result-replay-plan.md for scope, preconditions and negative envelopes.
"""

import argparse
import hashlib
import json
import ssl
from pathlib import Path

import httpx
from sqlalchemy import func, select

from resource_advisor.contracts import ExecutionResult, signature
from resource_advisor.store import Store, entities, jobs, outbox, usage


def snapshot(store, job_ids, project):
    with store.transaction() as conn:
        rows = [dict(store.job(conn, ref)) for ref in job_ids]
        assert all(row["project"] == project for row in rows)
        attempts = {row["body"]["attempt_id"] for row in rows}
        refs = attempts | {"artifact-" + ref for ref in attempts}
        records = [
            dict(row)
            for row in conn.execute(select(entities).order_by(entities.c.kind, entities.c.ref))
            .mappings()
            .all()
            if row["ref"] in refs
            or row["body"].get("job_id") in job_ids
            or (
                isinstance(row["body"].get("result"), dict)
                and row["body"]["result"].get("job_id") in job_ids
            )
        ]
        ledgers = [
            dict(row)
            for row in conn.execute(
                select(usage).where(usage.c.attempt_id.in_(attempts)).order_by(usage.c.attempt_id)
            ).mappings()
        ]
        events = [
            dict(row)
            for row in conn.execute(select(outbox).order_by(outbox.c.id)).mappings()
            if row["body"].get("job_id") in job_ids
            or row["body"].get("attempt_id") in attempts
            or row["body"].get("artifact_ref") in refs
        ]
        counts = {
            table.name: conn.scalar(select(func.count()).select_from(table))
            for table in (jobs, usage, outbox)
        }
        for kind in ("result", "profile"):
            counts[kind] = conn.scalar(
                select(func.count()).select_from(entities).where(entities.c.kind == kind)
            )
    assert len(ledgers) == len(rows) and all(e["status"] == "DONE" for e in events)
    assert all(row["project"] == project for row in records + ledgers)
    return {"jobs": rows, "records": records, "usage": ledgers, "outbox": events, "counts": counts}


def verify(config, report):
    with report.open("x"):
        pass
    report.chmod(0o600)
    saved = {"status": "INCOMPLETE", "cases": []}

    def save():
        report.write_text(json.dumps(saved, indent=2) + "\n")

    store = Store(config["database_url"])
    ids = [config["success_job_id"], config["canceled_job_id"]]
    assert len(set(ids)) == 2
    before = snapshot(store, ids, config["project"])
    saved["before"] = before
    success, canceled = before["jobs"]
    assert success["state"] == "SUCCEEDED" and canceled["state"] == "CANCELED"
    result_row = next(
        row
        for row in before["records"]
        if row["kind"] == "result" and row["ref"] == success["body"]["attempt_id"]
    )
    result = ExecutionResult.model_validate(result_row["body"])
    assert result.evidence_kind == "hardware" and result.outcome == "COMPLETED"
    assert signature(result) == success["body"]["result_digest"]
    assert not any(
        row["kind"] in {"result", "profile", "artifact"}
        and row["ref"]
        in {canceled["body"]["attempt_id"], "artifact-" + canceled["body"]["attempt_id"]}
        for row in before["records"]
    )
    save()
    with (
        httpx.Client(
            base_url=config["api_url"].rstrip("/") + "/api/v1/compute",
            verify=ssl.create_default_context(cafile=config["ca_file"]),
            timeout=20,
        ) as api,
        httpx.Client(base_url=config["mlflow_url"], timeout=20) as mlflow,
    ):

        def auth(key):
            return {"Authorization": "Bearer " + config[key]}

        def api_jobs():
            values = []
            for ref in ids:
                r = api.get("/jobs/" + ref, headers=auth("user_token"))
                r.raise_for_status()
                values.append(r.json())
            return values

        def artifacts():
            values = {}
            for job in before["jobs"]:
                r = api.get("/jobs/" + job["id"] + "/artifacts", headers=auth("user_token"))
                r.raise_for_status()
                rows = r.json()
                assert len(rows) == (1 if job["state"] == "SUCCEEDED" else 0)
                for item in rows:
                    data = api.get(
                        "/artifacts/" + item["ref"] + "/content", headers=auth("user_token")
                    )
                    data.raise_for_status()
                    assert data.json()["result"] == result.model_dump(mode="json")
                    values[item["ref"]] = hashlib.sha256(data.content).hexdigest()
            return values

        def tracking():
            values = {}
            for job in before["jobs"]:
                record = next(
                    row
                    for row in before["records"]
                    if row["kind"] == "tracking" and row["ref"] == job["body"]["attempt_id"]
                )["body"]
                query = {
                    "experiment_ids": [record["experiment_id"]],
                    "filter": "tags.`resource_advisor.attempt_id` = '"
                    + job["body"]["attempt_id"]
                    + "'",
                }
                r = mlflow.post("/api/2.0/mlflow/runs/search", json=query)
                r.raise_for_status()
                runs = r.json().get("runs", [])
                assert len(runs) == 1 and runs[0]["info"]["run_id"] == record["run_id"]
                expected = "FINISHED" if job["state"] == "SUCCEEDED" else "KILLED"
                assert runs[0]["info"]["status"] == expected
                tags = {tag["key"]: tag["value"] for tag in runs[0]["data"]["tags"]}
                assert tags["project"] == config["project"]
                assert tags["resource_advisor.job_id"] == job["id"]
                if job["state"] == "CANCELED":
                    assert not runs[0]["data"].get("metrics")
                values[job["id"]] = runs[0]
            return values

        saved["api_before"], saved["artifacts_before"], saved["mlflow_before"] = (
            api_jobs(),
            artifacts(),
            tracking(),
        )
        save()

        def send(name, envelope, token, expected, detail=None):
            # Capture intent before requesting; no blind retry after transport loss.
            case = {"name": name, "request": envelope.model_dump(mode="json"), "expected": expected}
            saved["cases"].append(case)
            save()
            r = api.post(
                "/results",
                headers={**auth(token), "X-Artifact-Digest": signature(envelope)},
                json=case["request"],
            )
            case.update(http=r.status_code, response=r.json())
            save()
            assert r.status_code == expected, name
            if detail is not None:
                assert case["response"]["detail"] == detail, name
            elif expected == 200:
                assert case["response"] == saved["api_before"][0], name

        for number in (1, 2):
            send("original-result-replay-" + str(number), result, "operator_token", 200)
        for key, value in (
            ("epoch", result.epoch + 1),
            ("attempt_id", "unrelated-rejection-probe"),
        ):
            send(
                "wrong-" + key,
                result.model_copy(update={key: value}),
                "operator_token",
                409,
                "stale or unrelated attempt",
            )
        send(
            "terminal-content-change",
            result.model_copy(update={"error_code": "REPLAY_NEGATIVE"}),
            "operator_token",
            409,
            "terminal result is immutable",
        )
        negative = ExecutionResult(
            job_id=canceled["id"],
            attempt_id=canceled["body"]["attempt_id"],
            epoch=canceled["epoch"],
            workload_signature=canceled["body"]["workload_signature"],
            context_signature=canceled["body"]["context_signature"],
            outcome="FAILED",
            evidence_kind="synthetic",
            error_code="REJECTION_PROBE_NOT_OBSERVED",
        )
        send(
            "late-after-confirmed-cancel",
            negative,
            "operator_token",
            409,
            "terminal result is immutable",
        )
        send(
            "late-cancel-wrong-epoch",
            negative.model_copy(update={"epoch": negative.epoch + 1}),
            "operator_token",
            409,
            "stale or unrelated attempt",
        )
        send("foreign-operator", result, "foreign_operator_token", 404, "job not found")
        send("researcher-ingestion", result, "user_token", 403, "Operator qualification required")
        saved["after"] = snapshot(store, ids, config["project"])
        saved["api_after"], saved["artifacts_after"], saved["mlflow_after"] = (
            api_jobs(),
            artifacts(),
            tracking(),
        )
        save()
        for name in ("", "api_", "artifacts_", "mlflow_"):
            assert saved[name + "before"] == saved[name + "after"], name + "changed"
        saved["status"] = "PASS"
        save()
        return saved


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = verify(json.loads(args.config.read_text()), args.report)
    print(result["status"], len(result["cases"]), "requests, unchanged terminal records")
