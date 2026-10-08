"""Native evidence boundary checks for the bounded JCT experiment."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from jct_native import (
    balanced_schedule,
    build_queues,
    normalize_slurm,
    parse_native_time,
    parse_squeue,
    result_line,
)

from resource_advisor.jct_selection import (
    CandidateIdentity,
    MeasuredProfile,
    SubmitIntent,
    select_candidate,
)


def test_pending_expected_start_is_not_an_observed_allocation_start():
    rows = parse_squeue(
        "91|exp-job|ra-lab|compute|ra-normal|PENDING||slurm-w2|2026-10-08T10:00:00+0000|2026-10-08T09:00:00+0000|Resources\n"
    )
    assert rows[0]["started_at"] is None
    assert rows[0]["expected_start_at"] == 1791453600.0


@pytest.mark.parametrize("value", ["2026-10-08T10:00:00", "%s", "garbage"])
def test_ambiguous_native_time_is_rejected(value):
    with pytest.raises(ValueError):
        parse_native_time(value)


def test_numeric_epoch_and_aware_offset_preserve_the_same_native_time():
    assert parse_native_time("1791453600") == parse_native_time("2026-10-08T19:00:00+0900")


def test_slurm_disappearance_or_foreign_account_cannot_become_success():
    item = {"attempt_id": "exp-job", "native_id": "91"}
    assert normalize_slurm([], item, "", {}) is None
    row = {
        "native_id": "91",
        "name": "exp-job",
        "account": "foreign",
        "qos": "ra-normal",
        "nodes": "slurm-w2",
        "state": "COMPLETED",
    }
    with pytest.raises(ValueError, match="identity"):
        normalize_slurm([row], item, "", {})


def test_duplicate_workload_result_is_rejected():
    with pytest.raises(ValueError, match="exactly one"):
        result_line("POOL_INFERENCE_RESULT {}\nPOOL_INFERENCE_RESULT {}\n")


def test_reversed_native_boundaries_are_retained_as_error():
    row = {
        "native_id": "91",
        "name": "exp-job",
        "account": "ra-lab",
        "qos": "ra-normal",
        "nodes": "slurm-w2",
        "state": "COMPLETED",
        "exit_code": "0:0",
        "accepted_at": 100.0,
        "started_at": 99.0,
        "finished_at": 110.0,
    }
    with pytest.raises(ValueError, match="reversed"):
        normalize_slurm([row], {"attempt_id": "exp-job", "native_id": "91"}, "", {})


def test_seeded_plan_has_27_complete_rotated_paired_cohorts():
    cohorts = balanced_schedule(20261008)
    assert len(cohorts) == 27
    for load in ("sparse", "moderate", "burst"):
        orders = [
            [c["arm"] for c in cohorts if c["load"] == load and c["block"] == block]
            for block in range(3)
        ]
        assert {tuple(order) for order in orders}.__len__() == 3
        assert all(
            set(order) == {"round_robin", "profile_only", "profile_queue"} for order in orders
        )


def test_terminal_quota_hold_is_not_idle_and_visible_intent_is_not_doubled():
    identity = CandidateIdentity(
        "gpu", "kubernetes", "lab", "node", "nvidia.com/gpu", "image", "work"
    )
    profile = MeasuredProfile(
        identity, "p", 90.0, 1.0, 1.0, 1.0, 0.0, 2.0, True, True, readmission_seconds=5.0
    )
    item = {"attempt_id": "own", "candidate_ref": "gpu", "native_id": "own"}
    job = {
        "metadata": {"name": "own", "uid": "u", "labels": {"resource-advisor/experiment": "exp"}},
        "status": {"succeeded": 1},
    }
    workload = {
        "metadata": {"ownerReferences": [{"kind": "Job", "uid": "u"}]},
        "status": {"conditions": [{"type": "QuotaReserved", "status": "True"}]},
    }
    snapshot = {
        "observed_at": 100.0,
        "snapshot_ref": "s",
        "jobs": [job],
        "pods": [],
        "workloads": [workload],
        "all_pods": [],
        "slurm_rows": [],
        "accounting_rows": [],
        "errors": [],
    }
    queues = build_queues(
        snapshot,
        [identity],
        [profile],
        [{"ref": "gpu", "node": "node", "nominal_slots": 1}],
        [item],
        "exp",
    )
    result = select_candidate(
        "profile_queue",
        [identity],
        [profile],
        now_seconds=100.0,
        queues=queues,
        intents=[SubmitIntent(identity, "own", 99.0, 2.0, 5.0)],
    )
    assert result.candidate_ref is None
    assert result.predictions[0].reasons == ("QUEUE_WORK_UNKNOWN",)
    workload["status"]["conditions"][0]["status"] = "False"
    queues = build_queues(
        snapshot,
        [identity],
        [profile],
        [{"ref": "gpu", "node": "node", "nominal_slots": 1}],
        [item],
        "exp",
    )
    result = select_candidate(
        "profile_queue",
        [identity],
        [profile],
        now_seconds=100.0,
        queues=queues,
        intents=[SubmitIntent(identity, "own", 99.0, 2.0, 5.0)],
    )
    assert result.predictions[0].queue_wait_seconds == 0.0
    assert result.predictions[0].deduplicated_intent_ids == ("own",)


def test_foreign_gpu_demand_makes_route_incomplete_instead_of_empty():
    identity = CandidateIdentity(
        "gpu", "kubernetes", "lab", "node", "nvidia.com/gpu", "image", "work"
    )
    profile = MeasuredProfile(identity, "p", 90.0, 1.0, 1.0, 1.0, 0.0, 2.0, True, True)
    pod = {
        "metadata": {"labels": {}},
        "spec": {
            "nodeName": "node",
            "containers": [{"resources": {"requests": {"nvidia.com/gpu": "1"}}}],
        },
        "status": {"phase": "Running"},
    }
    snapshot = {
        "observed_at": 100.0,
        "snapshot_ref": "s",
        "jobs": [],
        "pods": [],
        "workloads": [],
        "all_pods": [pod],
        "slurm_rows": [],
        "accounting_rows": [],
        "errors": [],
    }
    queues = build_queues(
        snapshot,
        [identity],
        [profile],
        [{"ref": "gpu", "node": "node", "nominal_slots": 1}],
        [],
        "exp",
    )
    assert queues[0].complete is False
    assert (
        select_candidate(
            "profile_queue", [identity], [profile], now_seconds=100.0, queues=queues
        ).candidate_ref
        is None
    )


def test_shared_two_jobs_do_not_prove_readmission_but_capacity_plus_one_does():
    from copy import deepcopy

    from jct_native import freeze_profiles

    plan = {
        "pool": [
            {
                "ref": "shared",
                "backend": "kubernetes",
                "node": "node",
                "resource_key": "nvidia.com/gpu.shared",
                "nominal_slots": 2,
                "image": "image",
            }
        ],
        "source_sha256": "source",
        "fixture_sha256": "fixture",
        "kernel_sha256": "kernel",
        "accuracy": 1.0,
        "main_rounds": 1,
    }

    def job(name, start, finish, compute):
        return {
            "candidate_ref": "shared",
            "attempt_id": name,
            "accepted_at": 100.0,
            "started_at": start,
            "finished_at": finish,
            "gpu_seconds": finish - start,
            "outcome": "SUCCEEDED",
            "details": {
                "measured": True,
                "outcome": "COMPLETED",
                "fixture_sha256": "fixture",
                "kernel_sha256": "kernel",
                "accuracy": 1.0,
                "rounds": 1,
                "images": 256,
                "quality": 1.0,
                "round_seconds": [compute],
                "elapsed_seconds": compute,
                "compute_started_at": start + 1.0,
                "compute_finished_at": start + 1.0 + compute,
            },
        }

    calibration = {
        "plan": plan,
        "calibration_completed_at": 140.0,
        "qualifications": [job("a", 101.0, 111.0, 8.0), job("b", 101.0, 111.0, 8.0)],
    }
    with pytest.raises(ValueError, match="capacity"):
        freeze_profiles(calibration)
    calibration["qualifications"].append(job("c", 123.0, 129.0, 4.0))
    _, profiles, raw = freeze_profiles(calibration)
    assert profiles[0].readmission_seconds == 12.0
    assert profiles[0].compute_seconds == 8.0
    assert raw["shared"]["raw_phases"][2]["compute"] == 4.0
    unqueued = deepcopy(calibration)
    unqueued["qualifications"][2]["accepted_at"] = 115.0
    _, profiles, _ = freeze_profiles(unqueued)
    assert profiles[0].readmission_seconds is None


def test_native_pending_preserves_its_original_idle_admission_estimate():
    identity = CandidateIdentity(
        "gpu", "kubernetes", "lab", "node", "nvidia.com/gpu", "image", "work"
    )
    profile = MeasuredProfile(
        identity, "p", 90.0, 1.0, 1.0, 1.0, 0.0, 2.0, True, True, readmission_seconds=5.0
    )
    item = {"attempt_id": "first", "candidate_ref": "gpu", "expected_admission_seconds": 1.0}
    job = {
        "metadata": {"name": "first", "uid": "u", "labels": {"resource-advisor/experiment": "exp"}},
        "status": {},
    }
    snapshot = {
        "observed_at": 100.0,
        "snapshot_ref": "s",
        "jobs": [job],
        "pods": [],
        "workloads": [],
        "all_pods": [],
        "slurm_rows": [],
        "accounting_rows": [],
        "errors": [],
    }
    queues = build_queues(
        snapshot,
        [identity],
        [profile],
        [{"ref": "gpu", "node": "node", "nominal_slots": 1}],
        [item],
        "exp",
    )
    assert queues[0].pending[0].expected_admission_seconds == 1.0
