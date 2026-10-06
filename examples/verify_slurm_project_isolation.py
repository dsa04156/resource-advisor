"""Frozen six-Job E6 Slurm priority/ownership acceptance, with private reports.

Config: api_url, ca_file, run_ref, observer_command argv and projects a/b.
Each project: project_ref, token, workload_ref, profiles normal/high, account,
user, partition. Observer accepts repeated --job IDs and emits inspect_slurm_jobs
JSON. Qualification, native limits, gateway probes and publication are separate.
"""

import argparse
import json
import ssl
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "RESULT_INVALID"}


def verify(config, report):
    if set(config["projects"]) != {"a", "b"}:
        raise ValueError("two project identities required")
    with report.open("x"):
        pass
    report.chmod(0o600)
    state = {"status": "INCOMPLETE", "intents": {}, "jobs": {}, "rounds": []}
    clients = {
        key: httpx.Client(
            base_url=config["api_url"].rstrip("/") + "/api/v1/compute",
            verify=ssl.create_default_context(cafile=config["ca_file"]),
            headers={"Authorization": "Bearer " + scope["token"]},
            timeout=20,
        )
        for key, scope in config["projects"].items()
    }

    def save():
        report.write_text(json.dumps(state, indent=2) + "\n")

    def api(project, method, path, expected=200, **kwargs):
        r = clients[project].request(method, path, **kwargs)
        if r.status_code != expected:
            raise ValueError((project, path, r.status_code, r.text))
        return r.json()

    def submit(label, project, priority, key):
        scope = config["projects"][project]
        body = {
            "workload_ref": scope["workload_ref"],
            "scheduling_profile_ref": scope["profiles"][priority],
            "mode": "observe",
        }
        headers = {"Idempotency-Key": config["run_ref"] + "-" + key}
        state["intents"][label] = {"project": project, "request": body, "key": headers}
        save()
        receipt = api(project, "POST", "/jobs", json=body, headers=headers)
        state["jobs"][label] = {"project": project, "receipt": receipt}
        save()
        again = api(project, "POST", "/jobs", json=body, headers=headers)
        assert (again["job_id"], again["attempt_id"]) == (receipt["job_id"], receipt["attempt_id"])

    def snapshot():
        ids = []
        for label, entry in state["jobs"].items():
            current = api(entry["project"], "GET", "/jobs/" + entry["receipt"]["job_id"])
            entry["latest"] = current
            if current.get("external_id"):
                ids.append(current["external_id"])
            elif current["state"] in TERMINAL:
                raise ValueError((label, current["state"], "no native receipt"))
        save()
        if not ids:
            return
        argv = list(config["observer_command"])
        for job in ids:
            argv += ["--job", job]
        observed = json.loads(subprocess.check_output(argv, text=True, timeout=40))
        for entry in state["jobs"].values():
            current, scope = entry["latest"], config["projects"][entry["project"]]
            if not current.get("external_id"):
                continue
            item = observed[current["external_id"]]
            native = item["native"]
            if native:
                assert native["Account"] == scope["account"]
                assert native["UserId"].split("(")[0] == scope["user"]
                assert native["Partition"] == scope["partition"]
                assert native["JobName"] == entry["receipt"]["attempt_id"]
            for row in item["accounting"]:
                assert row["Account"] == scope["account"] and row["User"] == scope["user"]
                assert row["JobName"] == entry["receipt"]["attempt_id"]
            entry["observation"] = item
        save()

    def until(predicate, seconds=600):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            snapshot()
            result = predicate()
            if result:
                return result
            time.sleep(3)
        raise TimeoutError("inspect the saved IDs; do not start another trial")

    def native(label):
        return state["jobs"][label].get("observation", {}).get("native", {})

    try:
        for project in clients:
            assert all(j["state"] in TERMINAL for j in api(project, "GET", "/jobs"))
        for number, (holder_project, other) in enumerate((("a", "b"), ("b", "a")), 1):
            holder, normal, high = [
                f"round-{number}-" + role for role in ("holder", "normal", "high")
            ]
            submit(holder, holder_project, "normal", f"round-{number}-shared")
            until(lambda holder=holder: native(holder).get("JobState") == "RUNNING")
            submit(normal, other, "normal", f"round-{number}-shared")
            assert (
                state["jobs"][holder]["receipt"]["job_id"]
                != state["jobs"][normal]["receipt"]["job_id"]
            )
            until(lambda normal=normal: native(normal).get("JobState") == "PENDING")
            submit(high, holder_project, "high", f"round-{number}-high")
            until(lambda high=high: native(high).get("JobState") == "PENDING")
            low, urgent = native(normal), native(high)
            assert low["QOS"] == "ra-normal" and urgent["QOS"] == "ra-high"
            assert int(urgent["Priority"]) > int(low["Priority"])
            assert low["SubmitTime"] <= urgent["SubmitTime"]
            for label, foreign in ((holder, other), (normal, holder_project)):
                path = "/jobs/" + state["jobs"][label]["receipt"]["job_id"]
                api(foreign, "GET", path, expected=404)
                api(foreign, "POST", path + "/cancel", expected=404)
            state["rounds"].append({"number": number, "both_pending": {normal: low, high: urgent}})
            save()
            until(lambda high=high: native(high).get("JobState") == "RUNNING")
            assert native(normal).get("JobState") == "PENDING"
            state["rounds"][-1]["high_running_normal_pending"] = {
                high: native(high),
                normal: native(normal),
            }
            save()
            for label in (holder, high, normal):
                until(lambda label=label: state["jobs"][label]["latest"]["state"] in TERMINAL)
                assert state["jobs"][label]["latest"]["state"] == "SUCCEEDED"
            print("Slurm priority round", number, "passed with three real GPU results", flush=True)
        snapshot()
        for entry in state["jobs"].values():
            rows = entry["observation"]["accounting"]
            assert (
                len(rows) == 1 and rows[0]["State"] == "COMPLETED" and rows[0]["ExitCode"] == "0:0"
            )
        for item in state["rounds"]:
            names = {
                role: f"round-{item['number']}-" + role for role in ("holder", "high", "normal")
            }
            rows = {k: state["jobs"][v]["observation"]["accounting"][0] for k, v in names.items()}
            assert datetime.fromisoformat(rows["holder"]["End"]) <= datetime.fromisoformat(
                rows["high"]["Start"]
            )
            assert datetime.fromisoformat(rows["high"]["End"]) <= datetime.fromisoformat(
                rows["normal"]["Start"]
            )
        state["status"] = "PASS"
        save()
        return state
    except Exception:
        for entry in state["jobs"].values():
            if entry.get("latest", {}).get("state") not in TERMINAL:
                try:
                    entry["cleanup"] = api(
                        entry["project"], "POST", "/jobs/" + entry["receipt"]["job_id"] + "/cancel"
                    )
                except (httpx.HTTPError, ValueError) as error:
                    entry["cleanup_error"] = type(error).__name__
        save()
        raise
    finally:
        for client in clients.values():
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    verify(json.loads(args.config.read_text()), args.report)
