"""Phase-structured finish-time estimates through the public selection boundary.

Worked literals only; fixtures are software inputs, not hardware measurements.
"""

from dataclasses import replace

from resource_advisor.jct_selection import (
    PROFILE_QUEUE,
    PROFILE_QUEUE_NETWORK,
    V2_PROFILE_ONLY,
    CandidateIdentity,
    MeasuredProfile,
    QueueSnapshot,
    WorkItem,
    select_candidate,
)


def candidate(ref):
    return CandidateIdentity(ref, "kubernetes", "lab", ref, "nvidia.com/gpu", "runtime", "work")


def phased(identity, phases, *, groups=(), admission=0.5):
    return MeasuredProfile(
        identity=identity,
        evidence_ref="measured-" + identity.candidate_ref,
        measured_at=90.0,
        compute_seconds=None,
        preparation_seconds=None,
        admission_seconds=admission,
        release_seconds=None,
        v2_service_seconds=None,
        qualified=True,
        quality_passed=True,
        readmission_seconds=admission,
        phases=tuple(phases.items()),
        overlap_groups=groups,
    )


def empty_queue(identity):
    return QueueSnapshot(identity, "q-" + identity.candidate_ref, 100.0, 1, complete=True)


def test_sequential_phases_add_once_and_overlapping_phases_use_the_critical_path():
    gpu = candidate("gpu")
    phases = {"runtime_startup": 2.0, "data_staging": 3.0, "model_preparation": 5.0, "compute": 4.0}
    serial = select_candidate(
        PROFILE_QUEUE, [gpu], [phased(gpu, phases)], now_seconds=100.0, queues=[empty_queue(gpu)]
    )
    overlapped = select_candidate(
        PROFILE_QUEUE,
        [gpu],
        [phased(gpu, phases, groups=(("data_staging", "model_preparation"),))],
        now_seconds=100.0,
        queues=[empty_queue(gpu)],
    )
    # 0 wait + 0.5 admission + 2 + 3 + 5 + 4.
    assert serial.predictions[0].predicted_jct_seconds == 14.5
    # Staging (3) runs under model preparation (5): 0.5 + 2 + 5 + 4.
    assert overlapped.predictions[0].predicted_jct_seconds == 11.5
    assert overlapped.predictions[0].critical_path_seconds == 11.0


def test_unknown_phase_needs_profile_instead_of_counting_as_zero():
    gpu = candidate("gpu")
    decision = select_candidate(
        PROFILE_QUEUE,
        [gpu],
        [phased(gpu, {"runtime_startup": 2.0, "compute": None})],
        now_seconds=100.0,
        queues=[empty_queue(gpu)],
    )
    assert decision.candidate_ref is None
    assert decision.predictions[0].reasons == ("PROFILE_COMPONENT_UNKNOWN",)


def test_profile_only_phases_ignore_queue_and_admission():
    gpu = candidate("gpu")
    decision = select_candidate(
        V2_PROFILE_ONLY,
        [gpu],
        [phased(gpu, {"runtime_startup": 1.0, "compute": 2.0})],
        now_seconds=100.0,
    )
    assert decision.predictions[0].predicted_jct_seconds == 3.0


def test_network_estimate_replaces_measured_staging_rather_than_adding_to_it():
    near, far = candidate("near"), candidate("far")
    phases = {"runtime_startup": 1.0, "data_staging": 9.0, "compute": 2.0}
    decision = select_candidate(
        PROFILE_QUEUE_NETWORK,
        [near, far],
        [phased(near, phases), phased(far, phases)],
        now_seconds=100.0,
        queues=[empty_queue(near), empty_queue(far)],
        transfer_seconds={"near": 0.5, "far": 6.0},
    )
    by_ref = {p.candidate_ref: p for p in decision.predictions}
    assert by_ref["near"].predicted_jct_seconds == 4.0
    assert by_ref["far"].predicted_jct_seconds == 9.5
    assert by_ref["near"].transfer_seconds == 0.5
    assert decision.candidate_ref == "near"


def test_unknown_link_or_inseparable_staging_abstains_for_network_policy():
    known, unknown, merged = candidate("known"), candidate("unknown"), candidate("merged")
    staged = {"data_staging": 1.0, "compute": 2.0}
    decision = select_candidate(
        PROFILE_QUEUE_NETWORK,
        [known, unknown, merged],
        [
            phased(known, staged),
            phased(unknown, staged),
            phased(merged, {"compute": 3.0}),
        ],
        now_seconds=100.0,
        queues=[empty_queue(known), empty_queue(unknown), empty_queue(merged)],
        transfer_seconds={"known": 1.5, "unknown": None, "merged": 0.1},
    )
    reasons = {p.candidate_ref: p.reasons for p in decision.predictions}
    assert reasons["unknown"] == ("NETWORK_ESTIMATE_UNKNOWN",)
    assert reasons["merged"] == ("STAGING_NOT_SEPARABLE",)
    assert decision.candidate_ref == "known"


def test_queue_wait_precedes_phases_and_is_never_double_counted():
    gpu = candidate("gpu")
    busy = replace(empty_queue(gpu), running=(WorkItem("other", 10.0, started_at=96.0),))
    decision = select_candidate(
        PROFILE_QUEUE,
        [gpu],
        [phased(gpu, {"compute": 2.0}, admission=0.5)],
        now_seconds=100.0,
        queues=[busy],
    )
    prediction = decision.predictions[0]
    assert prediction.queue_wait_seconds == 6.0
    assert prediction.predicted_jct_seconds == 8.5
