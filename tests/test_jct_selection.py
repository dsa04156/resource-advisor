"""Worked timing examples test the experimental public selection boundary."""

from dataclasses import replace

import pytest

from resource_advisor.jct_selection import (
    PROFILE_QUEUE,
    ROUND_ROBIN,
    V2_PROFILE_ONLY,
    CandidateIdentity,
    MeasuredProfile,
    QueueSnapshot,
    SubmitIntent,
    WorkItem,
    select_candidate,
)


def candidate(ref):
    return CandidateIdentity(ref, "kubernetes", "lab", ref, "nvidia.com/gpu", "runtime", "work")


def profile(identity, compute, *, preparation=1.0):
    return MeasuredProfile(
        identity=identity,
        evidence_ref="measured-" + identity.candidate_ref,
        measured_at=90.0,
        compute_seconds=compute,
        preparation_seconds=preparation,
        admission_seconds=0.5,
        release_seconds=0.5,
        v2_service_seconds=compute + preparation + 0.5,
        qualified=True,
        quality_passed=True,
        readmission_seconds=0.5,
    )


def queue(identity, *, running=(), pending=(), observed_at=100.0):
    return QueueSnapshot(
        identity=identity,
        snapshot_ref="queue-" + identity.candidate_ref,
        observed_at=observed_at,
        capacity=1,
        complete=True,
        running=running,
        pending=pending,
    )


def test_ready_slower_gpu_beats_fast_busy_gpu():
    fast, ready = candidate("fast"), candidate("ready")
    decision = select_candidate(
        PROFILE_QUEUE,
        [fast, ready],
        [profile(fast, 1.0), profile(ready, 4.0)],
        now_seconds=100.0,
        queues=[
            queue(fast, running=(WorkItem("busy", 20.0, started_at=95.0),)),
            queue(ready),
        ],
    )

    assert decision.candidate_ref == "ready"
    assert decision.predictions[0].queue_wait_seconds == 15.0
    assert decision.predictions[1].predicted_jct_seconds == 6.0


def test_stale_queue_cannot_be_treated_as_empty_capacity():
    fast, ready = candidate("fast"), candidate("ready")
    decision = select_candidate(
        PROFILE_QUEUE,
        [fast, ready],
        [profile(fast, 1.0), profile(ready, 4.0)],
        now_seconds=100.0,
        queues=[queue(fast, observed_at=94.0), queue(ready)],
    )

    assert decision.candidate_ref == "ready"
    assert decision.predictions[0].predicted_jct_seconds is None
    assert decision.predictions[0].reasons == ("QUEUE_STALE",)


@pytest.mark.parametrize(
    "queues,reason",
    [
        ([], "QUEUE_MISSING"),
        ([replace(queue(candidate("gpu")), complete=False)], "QUEUE_INCOMPLETE"),
        ([queue(candidate("gpu"), observed_at=101.0)], "QUEUE_FROM_FUTURE"),
        ([queue(replace(candidate("gpu"), cluster_ref="other"))], "QUEUE_IDENTITY_MISMATCH"),
    ],
)
def test_unknown_queue_abstains_with_auditable_reason(queues, reason):
    gpu = candidate("gpu")
    decision = select_candidate(
        PROFILE_QUEUE, [gpu], [profile(gpu, 1.0)], now_seconds=100.0, queues=queues
    )

    assert decision.candidate_ref is None
    assert decision.predictions[0].reasons == (reason,)
    assert decision.predictions[0].queue_wait_seconds is None


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"qualified": False}, "PROFILE_UNQUALIFIED"),
        ({"quality_passed": False}, "PROFILE_QUALITY_FAILED"),
        ({"synthetic": True}, "PROFILE_SYNTHETIC"),
        ({"measured_at": 101.0}, "PROFILE_FROM_FUTURE"),
        ({"measured_at": -86301.0}, "PROFILE_STALE"),
        ({"preparation_seconds": None}, "PROFILE_COMPONENT_UNKNOWN"),
        ({"release_seconds": -1.0}, "PROFILE_COMPONENT_INVALID"),
        ({"compute_seconds": float("nan")}, "PROFILE_COMPONENT_INVALID"),
        (
            {"identity": replace(candidate("gpu"), runtime_digest="other")},
            "PROFILE_IDENTITY_MISMATCH",
        ),
    ],
)
def test_unusable_profile_never_ranks_as_a_fast_gpu(change, reason):
    gpu = candidate("gpu")
    decision = select_candidate(
        PROFILE_QUEUE,
        [gpu],
        [replace(profile(gpu, 1.0), **change)],
        now_seconds=100.0,
        queues=[queue(gpu)],
    )

    assert decision.candidate_ref is None
    assert decision.predictions[0].reasons == (reason,)


def test_startup_cost_can_reverse_the_compute_only_preference_and_is_traced():
    fast, slow = candidate("fast"), candidate("slow")
    decision = select_candidate(
        PROFILE_QUEUE,
        [fast, slow],
        [profile(fast, 0.1, preparation=10.0), profile(slow, 2.0, preparation=0.0)],
        now_seconds=100.0,
        queues=[queue(fast), queue(slow)],
    )

    assert decision.candidate_ref == "slow"
    prediction = decision.predictions[1]
    assert prediction.compute_seconds == 2.0
    assert prediction.preparation_seconds == 0.0
    assert prediction.admission_seconds == 0.5
    assert prediction.release_seconds == 0.5
    assert prediction.profile_evidence_ref == "measured-slow"
    assert prediction.queue_snapshot_ref == "queue-slow"
    assert prediction.predicted_jct_seconds == 3.0


def test_pending_work_waits_for_running_residual_and_pays_readmission_once():
    gpu = candidate("gpu")
    decision = select_candidate(
        PROFILE_QUEUE,
        [gpu],
        [profile(gpu, 1.0)],
        now_seconds=100.0,
        queues=[
            queue(
                gpu,
                running=(WorkItem("running", 10.0, started_at=96.0),),
                pending=(WorkItem("pending", 2.0, expected_admission_seconds=1.0),),
            )
        ],
    )

    # Running has 6s left; next pending job occupies 2s after 1s readmission.
    assert decision.predictions[0].queue_wait_seconds == 9.0
    assert decision.predictions[0].predicted_jct_seconds == 12.0
    assert decision.predictions[0].accounted_job_ids == ("running", "pending")


def test_shared_capacity_uses_first_available_slot_without_claiming_two_physical_gpus():
    gpu = candidate("shared")
    decision = select_candidate(
        PROFILE_QUEUE,
        [gpu],
        [profile(gpu, 1.0)],
        now_seconds=100.0,
        queues=[
            replace(
                queue(
                    gpu,
                    running=(
                        WorkItem("first", 10.0, started_at=96.0),
                        WorkItem("second", 10.0, started_at=91.0),
                    ),
                    pending=(WorkItem("pending", 2.0, expected_admission_seconds=1.0),),
                ),
                capacity=2,
                capacity_unit="logical_slot",
            )
        ],
    )

    # Slots become free at 1s and 6s. Pending fills the first through 4s.
    assert decision.predictions[0].queue_wait_seconds == 4.0
    assert decision.predictions[0].capacity_unit == "logical_slot"
    assert decision.predictions[0].capacity == 2


@pytest.mark.parametrize(
    "snapshot,reason",
    [
        (replace(queue(candidate("gpu")), capacity=0), "QUEUE_CAPACITY_INVALID"),
        (
            queue(candidate("gpu"), running=(WorkItem("job", None, started_at=99.0),)),
            "QUEUE_WORK_UNKNOWN",
        ),
        (queue(candidate("gpu"), running=(WorkItem("job", 1.0),)), "RUNNING_START_UNKNOWN"),
        (
            queue(candidate("gpu"), running=(WorkItem("job", 1.0, started_at=101.0),)),
            "RUNNING_START_INVALID",
        ),
        (queue(candidate("gpu"), pending=(WorkItem("job", 1.0),)), "QUEUE_ADMISSION_UNKNOWN"),
        (
            queue(
                candidate("gpu"), pending=(WorkItem("job", -1.0, expected_admission_seconds=0.0),)
            ),
            "QUEUE_WORK_INVALID",
        ),
    ],
)
def test_incomplete_queue_timing_never_creates_a_zero_wait(snapshot, reason):
    gpu = candidate("gpu")
    decision = select_candidate(
        PROFILE_QUEUE, [gpu], [profile(gpu, 1.0)], now_seconds=100.0, queues=[snapshot]
    )

    assert decision.candidate_ref is None
    assert decision.predictions[0].reasons == (reason,)


def test_accepted_intent_adds_demand_before_native_visibility_but_is_not_counted_twice():
    gpu = candidate("gpu")
    intent = SubmitIntent(gpu, "submitted", 99.0, 2.0, 1.0)
    predictions = []
    for pending in [(), (WorkItem("submitted", 2.0, expected_admission_seconds=1.0),)]:
        result = select_candidate(
            PROFILE_QUEUE,
            [gpu],
            [profile(gpu, 1.0)],
            now_seconds=100.0,
            queues=[queue(gpu, pending=pending)],
            intents=[intent],
        )
        predictions.append(result.predictions[0])

    assert [p.queue_wait_seconds for p in predictions] == [3.0, 3.0]
    assert [p.accounted_job_ids for p in predictions] == [("submitted",), ("submitted",)]
    assert predictions[1].deduplicated_intent_ids == ("submitted",)


def test_controls_keep_round_robin_order_and_v2_static_cohort_backlog():
    fast, slow = candidate("fast"), candidate("slow")
    profiles = [profile(fast, 1.0), profile(slow, 4.0)]
    rr = select_candidate(ROUND_ROBIN, [slow, fast], profiles, now_seconds=100.0, rr_index=3)
    baseline = select_candidate(
        V2_PROFILE_ONLY,
        [slow, fast],
        profiles,
        now_seconds=100.0,
        cohort_backlog_seconds={"fast": 10.0, "slow": 0.0},
        queues=[queue(fast), queue(slow, observed_at=0.0)],
    )

    assert rr.candidate_ref == "fast"
    assert baseline.candidate_ref == "slow"
    assert baseline.predictions[0].predicted_jct_seconds == 5.5
    assert baseline.predictions[0].queue_snapshot_ref is None
    assert baseline.predictions[0].reasons == ("V2_STATIC_COHORT_BACKLOG",)


def test_busy_candidate_charges_readmission_instead_of_idle_admission_once():
    gpu = candidate("gpu")
    measured = replace(profile(gpu, 1.0), readmission_seconds=5.0)
    result = select_candidate(
        PROFILE_QUEUE,
        [gpu],
        [measured],
        now_seconds=100.0,
        queues=[queue(gpu, running=(WorkItem("running", 10.0, started_at=96.0),))],
    )

    assert result.predictions[0].queue_wait_seconds == 6.0
    assert result.predictions[0].admission_seconds == 5.0
    assert result.predictions[0].predicted_jct_seconds == 13.5


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"profiles": []}, "PROFILE_MISSING"),
        ({"profiles": [profile(candidate("gpu"), 1.0)] * 2}, "PROFILE_AMBIGUOUS"),
        ({"queues": [queue(candidate("gpu"))] * 2}, "QUEUE_AMBIGUOUS"),
        (
            {"queues": [replace(queue(candidate("gpu")), observed_at=float("nan"))]},
            "QUEUE_TIME_INVALID",
        ),
        (
            {"profiles": [replace(profile(candidate("gpu"), 1.0), measured_at=float("nan"))]},
            "PROFILE_TIME_INVALID",
        ),
        (
            {
                "queues": [
                    queue(candidate("gpu"), running=(WorkItem("late", 1.0, started_at=98.0),))
                ]
            },
            "RUNNING_EXPECTATION_EXCEEDED",
        ),
        (
            {
                "queues": [
                    queue(
                        candidate("gpu"),
                        running=(
                            WorkItem("same", 3.0, started_at=99.0),
                            WorkItem("same", 3.0, started_at=99.0),
                        ),
                    )
                ]
            },
            "QUEUE_JOB_ID_AMBIGUOUS",
        ),
        (
            {
                "profiles": [replace(profile(candidate("gpu"), 1.0), readmission_seconds=None)],
                "queues": [
                    queue(candidate("gpu"), running=(WorkItem("run", 3.0, started_at=99.0),))
                ],
            },
            "READMISSION_UNKNOWN_OR_INVALID",
        ),
    ],
)
def test_uncertain_state_is_not_silently_resolved_by_first_record_or_zero(change, reason):
    gpu = candidate("gpu")
    arguments = {"profiles": [profile(gpu, 1.0)], "queues": [queue(gpu)], **change}
    decision = select_candidate(PROFILE_QUEUE, [gpu], now_seconds=100.0, **arguments)

    assert decision.candidate_ref is None
    assert decision.predictions[0].reasons == (reason,)


def test_equal_predictions_have_stable_ties_and_archive_plain_json():
    import json
    from dataclasses import asdict

    a, b = candidate("a"), candidate("b")
    decision = select_candidate(
        PROFILE_QUEUE,
        [b, a],
        [profile(a, 1.0), profile(b, 1.0)],
        now_seconds=100.0,
        queues=[queue(a), queue(b)],
    )

    assert decision.candidate_ref == "a"
    assert json.loads(json.dumps(asdict(decision)))["candidate_ref"] == "a"


@pytest.mark.parametrize(
    "arguments",
    [
        {"policy": "unknown"},
        {"now_seconds": float("nan")},
        {"max_queue_age_seconds": -1.0},
        {"max_profile_age_seconds": float("inf")},
        {"candidates": [candidate("gpu"), candidate("gpu")]},
    ],
)
def test_invalid_selection_contract_is_rejected_before_a_choice(arguments):
    gpu = candidate("gpu")
    inputs = {
        "policy": PROFILE_QUEUE,
        "candidates": [gpu],
        "profiles": [profile(gpu, 1.0)],
        "now_seconds": 100.0,
        "queues": [queue(gpu)],
        **arguments,
    }

    with pytest.raises(ValueError):
        select_candidate(**inputs)


def test_terminal_native_observation_reconciles_an_unresolved_intent():
    gpu = candidate("gpu")
    decision = select_candidate(
        PROFILE_QUEUE,
        [gpu],
        [profile(gpu, 1.0)],
        now_seconds=100.0,
        queues=[replace(queue(gpu), observed_job_ids=("finished",))],
        intents=[SubmitIntent(gpu, "finished", 98.0, None, None)],
    )

    assert decision.predictions[0].queue_wait_seconds == 0.0
    assert decision.predictions[0].deduplicated_intent_ids == ("finished",)
