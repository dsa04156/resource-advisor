import json
import runpy
from pathlib import Path

import pytest

from resource_advisor.store import Store, jobs

verifier = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "examples/verify_slurm_response_recovery.py")
)


def test_capture_keeps_tracking_records_with_only_job_and_run_ids():
    store = Store("sqlite://")
    store.initialize()
    with store.transaction() as conn:
        conn.execute(
            jobs.insert().values(
                id="job-one",
                project="team-one",
                idempotency_key="key-one",
                state="SUCCEEDED",
                epoch=1,
                version=1,
                body={"attempt_id": "attempt-one"},
            )
        )
        store.put(
            conn,
            "tracking",
            "attempt-one",
            "team-one",
            {"job_id": "job-one", "run_id": "run-one", "experiment_id": "exp-one"},
        )
        store.put(conn, "artifact_tracking", "attempt-one", "team-one", {"path": "result.json"})
    captured = verifier["capture_attempt"](
        store, {"job_id": "job-one", "attempt_id": "attempt-one"}, "team-one"
    )
    assert captured["records"]["tracking"]["body"]["run_id"] == "run-one"
    assert captured["records"]["artifact_tracking"]["body"]["path"] == "result.json"
    persisted = json.loads(json.dumps(captured))
    assert persisted["records"]["tracking"]["body"]["run_id"] == "run-one"
    with pytest.raises(AssertionError):
        verifier["capture_attempt"](
            store, {"job_id": "job-one", "attempt_id": "attempt-one"}, "other-team"
        )


@pytest.mark.parametrize("calls", [[], ["attempt-one", "attempt-one"], ["other-attempt"]])
def test_recovery_evidence_rejects_missing_duplicate_or_unrelated_submit(calls):
    files = {
        "accepted-slurm-submit.json": json.dumps({"attempt": "attempt-one", "external_id": "1"}),
        "slurm-submit-attempts.jsonl": "\n".join(json.dumps({"attempt": a}) for a in calls),
        "worker-crash-request.json": json.dumps({"worker_pid": 12}),
    }
    with pytest.raises(AssertionError):
        verifier["verify_files"](files, "attempt-one")


def test_recovery_evidence_accepts_one_exact_submit_and_pid_receipt():
    receipt = {"attempt": "attempt-one", "external_id": "1"}
    files = {
        "accepted-slurm-submit.json": json.dumps(receipt),
        "slurm-submit-attempts.jsonl": json.dumps({"attempt": "attempt-one"}) + "\n",
        "worker-crash-request.json": json.dumps({"worker_pid": 12}),
    }
    assert verifier["verify_files"](files, "attempt-one") == receipt
