"""Bounded real-API recommendation demo. Requires a qualified GPU workload and worker.

Use a new run ID and output path. Do not retry an interrupted run blindly: inspect
its saved job IDs first. Job keys are deterministic; this script also checks replay.
The output is private raw evidence. Sanitize infrastructure identifiers before publication.
"""

import argparse
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def verify(client, workload, baseline, run_id, output, *, deadline_seconds=300):
    if output.exists():
        raise ValueError("output exists; inspect saved execution IDs before starting another demo")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,70}", run_id):
        raise ValueError("run ID must be a short stable identifier")
    report = {
        "run_id": run_id,
        "workload_ref": workload,
        "started_at": datetime.now(UTC).isoformat(),
        "observations": [],
        "stages": [],
    }
    end = time.monotonic() + deadline_seconds

    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        # Restrict permissions before writing any potentially private runtime evidence.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(report, f, indent=2)

    def post(path, body, key=None, expected=200):
        headers = {"Idempotency-Key": key} if key else {}
        response = client.post("/api/v1/compute/" + path, json=body, headers=headers)
        if response.status_code != expected:
            raise RuntimeError(f"{path}: expected HTTP {expected}, got {response.status_code}")
        return response.json()

    def get(path):
        response = client.get("/api/v1/compute/" + path)
        response.raise_for_status()
        return response.json()

    def await_job(job):
        while time.monotonic() < end:
            status = get("jobs/" + job["job_id"])
            if status["state"] in {"SUCCEEDED", "FAILED", "CANCELED", "RESULT_INVALID"}:
                if status["state"] != "SUCCEEDED":
                    raise RuntimeError(
                        "existing job is terminal without a valid result: " + status["state"]
                    )
                return status
            time.sleep(1)
        raise TimeoutError(
            "deadline reached; inspect existing job, do not resubmit with another key"
        )

    save()
    try:
        before = get("overview")["jobs"]["total"]
        report["initial_recommendation"] = post("recommendations", {"workload_ref": workload})
        if report["initial_recommendation"]["status"] != "NEEDS_PROFILE":
            raise RuntimeError(
                "demo requires a new, compatible workload/context without sufficient history"
            )
        report["stages"].append("abstained_without_profile")
        save()
        post(
            "jobs",
            {
                "workload_ref": workload,
                "candidate_ref": baseline,
                "approval_ref": run_id + "-missing",
            },
            run_id + "-invalid",
            expected=404,
        )
        assert get("overview")["jobs"]["total"] == before
        report["invalid_baseline_approval"] = {"http_status": 404, "job_created": False}
        save()
        for index in range(3):
            key = f"{run_id}-observe-{index}"
            job = post(
                "jobs",
                {"workload_ref": workload, "candidate_ref": baseline, "mode": "observe"},
                key,
            )
            report["observations"].append(job)
            save()  # Save accepted IDs before waiting, including on interruption.
            report["observations"][-1] = await_job(job)
            save()
        rec = post("recommendations", {"workload_ref": workload})
        report["recommendation"] = rec
        save()
        if not rec["measured"] or not rec["candidate_ref"]:
            raise RuntimeError("measured recommendation unavailable; keep abstention")
        evidence = get("recommendations/" + rec["ref"] + "/evidence")
        supporting = {e["attempt_id"] for e in evidence["evidence_runs"]}
        expected_attempts = {j["attempt_id"] for j in report["observations"]}
        assert supporting == expected_attempts
        assert all(
            e["result"] and e["result"]["evidence_kind"] == "hardware"
            for e in evidence["evidence_runs"]
        )
        approval = post(
            "recommendations/" + rec["ref"] + "/approve",
            {"recommendation_digest": rec["digest"], "candidate_ref": rec["candidate_ref"]},
        )
        report["approval"] = approval
        request = {
            "workload_ref": workload,
            "candidate_ref": rec["candidate_ref"],
            "mode": "fixed",
            "approval_ref": approval["ref"],
        }
        report["approved_job"] = post("jobs", request, run_id + "-approved")
        save()
        report["approved_job"] = await_job(report["approved_job"])
        replay = post("jobs", request, run_id + "-approved")
        assert replay["job_id"] == report["approved_job"]["job_id"]
        post("jobs", dict(request, mode="observe"), run_id + "-approved", expected=409)
        report["idempotency"] = {"same_job_on_replay": True, "changed_request_http_status": 409}
        final = get("recommendations/" + rec["ref"] + "/evidence")
        assert len(final["approved_executions"]) == 1
        comparison = final["approved_executions"][0]
        assert comparison["comparison_status"] == "independent_measured"
        assert comparison["job"]["attempt_id"] not in supporting
        assert comparison["actual_seconds"] > 0
        report["evidence"] = final
        report["new_job_count"] = get("overview")["jobs"]["total"] - before
        assert report["new_job_count"] == 4
        report["stages"].extend(
            [
                "three_independent_gpu_observations",
                "measured_recommendation",
                "immutable_approval",
                "new_gpu_execution",
                "independent_comparison",
            ]
        )
        report["completed_at"] = datetime.now(UTC).isoformat()
        save()
        return report
    except (httpx.HTTPError, RuntimeError, TimeoutError, AssertionError, KeyError) as exc:
        report["error_type"] = type(exc).__name__
        save()
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--token-env", default="RA_TOKEN")
    parser.add_argument("--ca-file")
    parser.add_argument("--workload", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with httpx.Client(
        base_url=args.api_url,
        verify=args.ca_file or True,
        timeout=15,
        headers={"Authorization": "Bearer " + os.environ[args.token_env]},
    ) as client:
        report = verify(client, args.workload, args.baseline, args.run_id, args.output)
    print(
        json.dumps(
            {
                "completed": True,
                "new_gpu_jobs": report["new_job_count"],
                "independent_comparison": True,
                "speedup_claimed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
