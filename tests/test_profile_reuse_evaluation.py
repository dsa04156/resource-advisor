"""Hand-worked timing/cost examples, not GPU qualification."""

import copy

import pytest

from resource_advisor.profile_reuse_evaluation import summarize


def paired_capture():
    job = dict(
        accepted_at=0,
        started_at=1,
        finished_at=10,
        gpu_seconds=8,
        quality=1,
        outcome="SUCCEEDED",
        units=100,
        sensor=[],
    )
    return {
        "profiling": {"gpu_seconds": 3, "wall_seconds": 4, "source_attempts": ["p"]},
        "cohorts": [
            {"block": 0, "arm": arm, "jobs": [dict(job, attempt_id=arm)], "wall_seconds": 10}
            for arm in ("baseline", "reuse")
        ],
    }


@pytest.mark.parametrize(
    "failure",
    [
        "source",
        "duplicate",
        "failed",
        "unknown",
        "clock",
        "work",
        "input",
        "profile_cost",
        "duplicate_block",
    ],
)
def test_rejects_uncontrolled_or_incomplete_comparisons(failure):
    capture = paired_capture()
    target = capture["cohorts"][1]["jobs"][0]
    if failure == "source":
        target["attempt_id"] = "p"
    elif failure == "duplicate":
        target["attempt_id"] = "baseline"
    elif failure == "failed":
        target["outcome"] = "FAILED"
    elif failure == "unknown":
        target["gpu_seconds"] = None
    elif failure == "clock":
        target["started_at"] = 12
    elif failure == "work":
        target["units"] = 200
    elif failure == "input":
        capture["cohorts"][0]["jobs"][0]["details"] = {"input_digest": "a"}
        target["details"] = {"input_digest": "b"}
    elif failure == "profile_cost":
        capture["profiling"]["gpu_seconds"] = -1
    else:
        extra = copy.deepcopy(capture["cohorts"])
        for cohort in extra:
            cohort["jobs"][0]["attempt_id"] += "-other"
        capture["cohorts"].extend(extra)
    with pytest.raises(ValueError):
        summarize(capture)


def test_observed_cohorts_charge_profile_once_and_find_actual_payback():
    def job(ref, finish, reservation):
        return dict(
            attempt_id=ref,
            accepted_at=0,
            started_at=1,
            finished_at=finish,
            gpu_seconds=reservation,
            quality=1,
            outcome="SUCCEEDED",
            units=100,
            sensor=[],
        )

    capture = {
        "profiling": {"gpu_seconds": 3, "wall_seconds": 4, "source_attempts": ["p"]},
        "cohorts": [
            {"block": b, "arm": arm, "jobs": [job(f"{arm}-{b}", end, gpu)], "wall_seconds": end}
            for b in range(2)
            for arm, end, gpu in [("baseline", 10, 8), ("reuse", 8, 6)]
        ],
    }
    result = summarize(capture)
    assert result["arms"]["baseline"]["mean_jct_seconds"] == 10
    assert result["arms"]["reuse"]["mean_wait_seconds"] == 1
    assert result["improvements_percent"]["mean_jct_seconds"] == 20
    assert result["improvements_percent"]["throughput_jobs_per_hour"] == 25
    assert result["observed_gpu_payback_jobs"] == 2
    assert result["observed_wall_payback_jobs"] == 2
    assert result["cumulative"][-1]["reuse_with_profile_gpu_seconds"] == 15
    assert result["arms"]["reuse"]["nvml_utilization_percent"] is None


def test_pool_samples_include_observed_idle_and_do_not_fill_gaps():
    capture = paired_capture()
    capture["cohorts"][0].update(started_at=0, wall_seconds=10)
    capture["cohorts"][1].update(started_at=20, wall_seconds=10)
    capture["pool_sensor"] = [
        {"at": t, "utilization_percent": u}
        for t, u in [
            (0, 0),
            (2, 100),
            (4, 100),
            (6, 0),
            (8, 0),
            (10, 0),
            (20, 100),
            (22, 100),
            (30, 100),
        ]
    ]
    result = summarize(capture)
    assert result["arms"]["baseline"]["dcgm_utilization_percent"] == 40
    assert result["arms"]["baseline"]["dcgm_coverage_percent"] == 100
    assert result["arms"]["reuse"]["dcgm_utilization_percent"] == 100
    assert result["arms"]["reuse"]["dcgm_coverage_percent"] == 20
