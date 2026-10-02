"""Full admission/ask/execute/observe tests with explicit scheduler/sensor doubles.

Hardware-shaped records exercise validation; none are actual GPU measurements.
"""

import hashlib
import time
from collections import Counter
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_fidelity_space import space_fixture as shared_space_fixture  # noqa: F401
from test_fidelity_space import with_sampling_fixture
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation
from resource_advisor.contracts import (
    State,
    StudyRequest,
    ThermalPolicy,
    WorkloadSpec,
    now,
    signature,
)
from resource_advisor.fidelity_qualification import (
    FidelityQualificationPlan,
    FidelityQualifications,
)
from resource_advisor.fidelity_space import FidelitySpaces
from resource_advisor.sampling import SamplingBindingRequest, SamplingPolicies
from resource_advisor.service import NotFound, Rejected, required
from resource_advisor.study import Studies
from resource_advisor.thermal import Reading, ThermalTrace, Window
from resource_advisor.worker import Worker


@pytest.fixture
def qualified_space(request, bundle):
    service, _, space_request = request.getfixturevalue("shared_space_fixture")
    policy = ThermalPolicy(
        driver_version="test-driver",
        device_uuid_digest=signature("test-gpu"),
        maximum_temperature_c=80,
        maximum_gap_seconds=0.5,
        maximum_query_seconds=0.1,
    )
    versions = {**bundle[3].runtime_versions, "driver": policy.driver_version}
    cap = bundle[3].model_copy(update={"ref": "qualified-cap", "runtime_versions": versions})
    service.register("capability", cap, "team-a")
    for old_ref in ["short", "full"]:
        with service.store.transaction() as conn:
            original = WorkloadSpec.model_validate(
                required(service.store, conn, "workload", old_ref, "team-a")
            )
            variant = service.bundle(conn, "team-a", old_ref, "base")[2]
        ref = "qualified-" + old_ref
        variant = variant.model_copy(
            update={
                "ref": "v-" + ref,
                "workload_ref": ref,
                "runtime_versions": versions,
                "thermal_policy": policy,
            }
        )
        candidates = tuple(
            c.model_copy(
                update={
                    "variant_ref": variant.ref,
                    "capability_ref": cap.ref,
                    "context": c.context.model_copy(update={"runtime_versions": versions}),
                }
            )
            for c in original.candidates
        )
        spec = original.model_copy(update={"ref": ref, "candidates": candidates})
        service.register("variant", variant, "team-a")
        service.register("workload", spec, "team-a")
        SamplingPolicies(service).bind(
            "team-a", SamplingBindingRequest(workload_ref=ref, policy_ref="sample-policy")
        )
    space = FidelitySpaces(service).create(
        "team-a",
        space_request.model_copy(
            update={
                "ref": "qualified-space",
                "target_workload_ref": "qualified-full",
                "lower_workload_refs": ("qualified-short",),
            }
        ),
    )
    return service, space


class ThermalSchedulerDouble(SchedulerDouble):
    fault = None
    ratio = 2.0

    def result(self, job):
        result = super().result(job)["result"]
        units = job["body"]["spec"]["identity"]["work_units"]
        base = job["body"]["candidate"]["ref"] == "base"
        seconds = 0.0001 if base else 0.0001 * self.ratio
        if self.fault == "reversal" and units == 10:
            seconds = 0.0002 if base else 0.0001
        if self.fault == "bias" and units == 10:
            seconds *= 1.5
        if self.fault == "tie":
            seconds = 0.0001
        if self.fault == "drift":
            seconds *= 2
        if self.fault == "rank":
            seconds = 0.000116 if base else 0.0001
        elapsed = seconds * units
        # Ensure test wall cost exceeds the invented timed-forward value.
        time.sleep(elapsed + 0.001)
        result["evidence_kind"] = "hardware"  # schema validator fixture only
        result["measurements"].update(work_units=units, sample_count=units, elapsed_seconds=elapsed)
        e = with_sampling_fixture(job, result)
        policy = job["body"]["variant"]["thermal_policy"]

        def reading(t):
            return Reading(
                started=t,
                finished=t + 1e-6,
                temperature_c=45,
                power_w=30,
                sm_clock_mhz=1500,
                event_reasons=0,
                supported_reasons=511,
            )

        windows = tuple(
            Window(
                sample_ref=s["ref"],
                before=reading(i * (seconds + 1e-5)),
                forward_started=i * (seconds + 1e-5) + 2e-6,
                forward_finished=i * (seconds + 1e-5) + 2e-6 + seconds,
                after=reading(i * (seconds + 1e-5) + 3e-6 + seconds),
            )
            for i, s in enumerate(job["body"]["sampling_binding"]["plan"]["samples"])
        )
        e["thermal_trace"] = ThermalTrace(
            job_id=job["id"],
            attempt_id=job["body"]["attempt_id"],
            result_digest=signature(result),
            policy_digest=signature(policy),
            driver_version=policy["driver_version"],
            device_uuid_digest=policy["device_uuid_digest"],
            windows=windows,
        ).model_dump(mode="json")
        return e


def run_study(service, ref, worker):
    for _ in range(150):
        Studies(service).tick(ref)  # coordinator restart at every transition
        worker.submit_one()
        worker.reconcile_all()
        study = Studies(service).get("team-a", ref)
        if study["state"] in {"COMPLETED", "ABSTAINED", "FAILED", "CANCELED"}:
            return study
    raise AssertionError("study did not stop")


def calibrate(qualified_space, fault=None, minimum=0.001, ratio=2.0):
    service, space = qualified_space
    plan = FidelityQualificationPlan(
        ref="qualification",
        project_ref="team-a",
        space_ref=space["request"]["ref"],
        minimum_target_elapsed_seconds=minimum,
    )
    saved = FidelityQualifications(service).create("team-a", plan)
    assert FidelityQualifications(service).create("team-a", plan) == saved
    request = StudyRequest(
        workload_ref="qualified-full",
        strategy="fidelity_calibration",
        seed=7,
        fidelity_space_ref=space["request"]["ref"],
        fidelity_qualification_ref=plan.ref,
    )
    study = Studies(service).create("team-a", request, "calibration")
    assert Studies(service).create("team-a", request, "calibration")["ref"] == study["ref"]
    with pytest.raises(Rejected, match="completed"):
        FidelityQualifications(service).assess("team-a", plan.ref)
    backend = ThermalSchedulerDouble()
    backend.fault = fault
    backend.ratio = ratio
    backend.observation = Observation(State.COLLECTING)
    worker = Worker(service, {("team-a", "lab"): backend})
    result = run_study(service, study["ref"], worker)
    assert result["state"] == "COMPLETED", result
    assessment = FidelityQualifications(service).assess("team-a", plan.ref)
    assert FidelityQualifications(service).assess("team-a", plan.ref) == assessment
    return service, space, saved, assessment, result, worker, backend


def mf_request(space):
    return StudyRequest(
        workload_ref="qualified-full",
        strategy="mfkg",
        seed=7,
        fidelity_space_ref=space["request"]["ref"],
        fidelity_qualification_ref="qualification",
    )


def test_real_kernel_drives_mixed_jobs_and_independent_confirmation(qualified_space, monkeypatch):
    from resource_advisor.mfkg import ask_mfkg

    service, space, _, assessment, calibration, worker, _ = calibrate(qualified_space)
    assert assessment["status"] == "QUALIFIED" and not assessment["early_pruning_authorized"]
    seen = []

    def checked_ask(problem):
        seen.append({o.attempt_id for o in problem.observations})
        return ask_mfkg(problem)

    monkeypatch.setattr("resource_advisor.mfkg.ask_mfkg", checked_ask)
    study = Studies(service).create("team-a", mf_request(space), "mf")
    result = run_study(service, study["ref"], worker)
    assert result["state"] == "COMPLETED", result
    probes = [o for o in result["observations"] if o["mode"] == "pilot"]
    confirms = [o for o in result["observations"] if o["mode"] == "confirmation"]
    assert len(probes) >= 2 and len(confirms) == 6
    calibration_ids = {o["attempt_id"] for o in calibration["observations"] if o["mode"] == "pilot"}
    held_out = {
        o["attempt_id"]
        for o in calibration["observations"] + confirms
        if o["mode"] == "confirmation"
    }
    assert seen[0] == calibration_ids
    assert seen[1] == calibration_ids | {probes[0]["attempt_id"]}
    assert all(ids.isdisjoint(held_out) for ids in seen)
    assert result["historical_calibration_cost"] == calibration["recommendation"]["cost"]
    assert result["historical_calibration_recharged"] is False and result["planning_seconds"] > 0
    assert result["charged_device_seconds"] != calibration["charged_device_seconds"]
    assert {o["workload_ref"] for o in confirms} == {"qualified-full"}
    assert result["recommendation"]["confirmation_run_ids"] == [o["attempt_id"] for o in confirms]
    with service.store.transaction() as conn:
        for obs in probes:
            plan = required(service.store, conn, "probe_plan", obs["plan_ref"], "team-a")
            assert (
                plan["choice"]["method"]
                == "finite_space_cost_aware_qMultiFidelityKnowledgeGradient"
            )
            assert plan["choice"]["option_ref"] == obs["option_ref"]
            assert plan["choice"]["qualification_digest"] == signature(assessment)
            assert service.store.get(conn, "profile", obs["attempt_id"]) is None


@pytest.mark.parametrize(
    "fault,minimum,reason",
    [
        ("reversal", 0.001, "PAIRED_RANK_REVERSAL"),
        ("bias", 0.001, "PAIRED_BIAS_EXCEEDS_LIMIT"),
        ("tie", 0.001, "NO_RESOLVED_CONFIGURATION_ORDERING"),
        (None, 0.05, "TARGET_MEASUREMENT_TOO_SHORT"),
    ],
)
def test_observed_pairs_must_pass_preregistered_limits(qualified_space, fault, minimum, reason):
    service, space, _, assessment, _, _, _ = calibrate(qualified_space, fault, minimum)
    assert assessment["status"] == "REJECTED" and reason in assessment["reasons"]
    with pytest.raises(Rejected, match="MF_KG_DISABLED"):
        Studies(service).create("team-a", mf_request(space), "mf")


def test_model_failure_uses_new_target_confirmations_without_fabricated_probes(
    qualified_space, monkeypatch
):
    service, space, _, _, _, worker, _ = calibrate(qualified_space)

    def failure(_):
        raise ArithmeticError("test numerical failure")

    monkeypatch.setattr("resource_advisor.mfkg.ask_mfkg", failure)
    study = Studies(service).create("team-a", mf_request(space), "mf")
    result = run_study(service, study["ref"], worker)
    assert result["state"] == "COMPLETED"
    assert result["exploration_stop_reason"] == "MF_KG_MODEL_FAILED:ArithmeticError"
    assert Counter(o["mode"] for o in result["observations"]) == {"confirmation": 6}
    assert result["last_mf_analysis"]["analysis"] is None


def test_expiry_rechecked_between_plan_and_submission(qualified_space, monkeypatch):
    import resource_advisor.fidelity_qualification as module

    service, space, _, _, _, _, _ = calibrate(qualified_space)
    study = Studies(service).create("team-a", mf_request(space), "mf")
    Studies(service).tick(study["ref"])
    monkeypatch.setattr(module, "now", lambda: now() + timedelta(hours=2))
    Studies(service).tick(study["ref"])
    result = Studies(service).get("team-a", study["ref"])
    assert result["state"] == "ABSTAINED" and "PROBE_REJECTED" in result["stop_reason"]
    assert result["observations"] == []


def test_expiry_rechecked_before_external_backend_submission(qualified_space, monkeypatch):
    import resource_advisor.fidelity_qualification as module

    service, space, _, _, _, worker, backend = calibrate(qualified_space)
    study = Studies(service).create("team-a", mf_request(space), "mf")
    Studies(service).tick(study["ref"])
    Studies(service).tick(study["ref"])
    before = backend.submissions
    monkeypatch.setattr(module, "now", lambda: now() + timedelta(hours=2))
    worker.submit_one()
    assert backend.submissions == before
    Studies(service).tick(study["ref"])
    Studies(service).tick(study["ref"])
    assert Studies(service).get("team-a", study["ref"])["state"] == "ABSTAINED"


def test_live_observation_drift_stops_before_next_model_call(qualified_space):
    service, space, _, _, _, worker, backend = calibrate(qualified_space)
    backend.fault = "drift"
    study = Studies(service).create("team-a", mf_request(space), "mf")
    result = run_study(service, study["ref"], worker)
    assert result["state"] == "ABSTAINED"
    assert result["stop_reason"] == "MF_OBSERVATION_OUTSIDE_QUALIFIED_RANGE"
    assert len(result["observations"]) == 1
    with pytest.raises(Rejected, match="invalidated"):
        Studies(service).create("team-a", mf_request(space), "mf-after-drift")


def test_rank_change_within_latency_envelope_revokes_qualification(qualified_space):
    service, space, _, _, _, worker, backend = calibrate(qualified_space, ratio=1.1)
    backend.fault = "rank"
    study = Studies(service).create("team-a", mf_request(space), "mf")
    result = run_study(service, study["ref"], worker)
    assert result["state"] == "ABSTAINED"
    assert result["stop_reason"] == "MF_RANK_RELATION_CHANGED"
    status = FidelityQualifications(service).status("team-a", "qualification")
    assert not status["currently_eligible"]
    assert status["invalidation"]["attempt_id"] == result["observations"][0]["attempt_id"]
    assert status["assessment"]["status"] == "QUALIFIED"  # original evidence is retained


def test_qualification_before_completion_and_expiry_status(qualified_space, monkeypatch):
    import resource_advisor.fidelity_qualification as module

    service, space, _, _, _, _, _ = calibrate(qualified_space)
    status = FidelityQualifications(service).status("team-a", "qualification")
    assert status["currently_eligible"] and status["invalidation"] is None
    monkeypatch.setattr(module, "now", lambda: now() + timedelta(hours=2))
    status = FidelityQualifications(service).status("team-a", "qualification")
    assert not status["currently_eligible"] and "expired" in status["ineligibility_reason"]
    with pytest.raises(Rejected, match="expired"):
        Studies(service).create("team-a", mf_request(space), "mf")


def test_plan_is_single_trial_immutable_project_scoped_and_operator_only(qualified_space):
    service, space, plan, _, _, _, _ = calibrate(qualified_space)
    with pytest.raises(Rejected, match="single calibration"):
        Studies(service).create(
            "team-a",
            StudyRequest(
                workload_ref="qualified-full",
                strategy="fidelity_calibration",
                fidelity_space_ref=space["request"]["ref"],
                fidelity_qualification_ref="qualification",
            ),
            "another-calibration",
        )
    with pytest.raises(Rejected, match="immutable"):
        FidelityQualifications(service).create(
            "team-a",
            FidelityQualificationPlan.model_validate(
                {**plan["request"], "maximum_relative_bias": 0.3}
            ),
        )
    with pytest.raises(NotFound):
        FidelityQualifications(service).get("team-b", "qualification")
    tokens = {
        hashlib.sha256(t.encode()).hexdigest(): p
        for t, p in {
            "user": Principal("team-a"),
            "operator": Principal("team-a", True),
            "other": Principal("team-b"),
        }.items()
    }
    with TestClient(create_app(service, tokens)) as client:
        root = "/api/v1/compute/fidelity-qualifications"
        assert (
            client.post(
                root, json=plan["request"], headers={"Authorization": "Bearer user"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                root, json=plan["request"], headers={"Authorization": "Bearer operator"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                root + "/qualification", headers={"Authorization": "Bearer other"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                root + "/qualification/assessment", headers={"Authorization": "Bearer other"}
            ).status_code
            == 404
        )


def test_legacy_unregistered_calibration_cannot_be_retroactively_qualified(request):
    service, spaces, command = request.getfixturevalue("shared_space_fixture")
    spaces.create("team-a", command)
    with pytest.raises(Rejected, match="thermal policy"):
        FidelityQualifications(service).create(
            "team-a",
            FidelityQualificationPlan(ref="late", project_ref="team-a", space_ref=command.ref),
        )
    with pytest.raises(NotFound):
        FidelityQualifications(service).assess("team-a", "late")
