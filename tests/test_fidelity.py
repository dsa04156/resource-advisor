"""Calibration contracts with explicit test doubles; not hardware evidence."""

import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from resource_advisor.api import Principal, create_app
from resource_advisor.contracts import (
    ExecutionResult,
    JobRequest,
    Measurements,
    ProfilingPolicy,
    State,
    now,
    signature,
)
from resource_advisor.fidelity import CalibrationCell, FidelityCalibration, FidelityPlan
from resource_advisor.service import NotFound, Rejected
from resource_advisor.store import jobs


def prepare(service, bundle, *, mismatched_shape=False, consent=True):
    spec, candidate, variant, _ = bundle
    cells = []
    for cpu in [1, 2]:
        for phase, units in [("F1", 10), ("F2", 20)]:
            ref = f"paired-{cpu}-{phase}"
            identity = spec.identity.model_copy(update={"work_units": units})
            if mismatched_shape and phase == "F2":
                identity = identity.model_copy(update={"input_shape": (1, 3, 64, 64)})
            v = variant.model_copy(
                update={
                    "ref": "v-" + ref,
                    "workload_ref": ref,
                    "workload_signature": signature(identity),
                    "supported_shapes": (identity.input_shape,),
                }
            )
            context = candidate.context.model_copy(
                update={
                    "resources": candidate.context.resources.model_copy(update={"host_cpu": cpu})
                }
            )
            c = candidate.model_copy(update={"variant_ref": v.ref, "context": context})
            s = spec.model_copy(
                update={
                    "ref": ref,
                    "identity": identity,
                    "candidates": (c,),
                    "profiling": ProfilingPolicy(consent=consent),
                    "execution": spec.execution.model_copy(update={"max_run_seconds": 10}),
                }
            )
            service.register("variant", v, "team-a")
            service.register("workload", s, "team-a")
            cells.append(
                CalibrationCell(arm=f"cpu{cpu}", phase=phase, workload_ref=ref, candidate_ref=c.ref)
            )
            if phase == "F2":
                cells.append(cells[-1].model_copy(update={"phase": "F3"}))
    return FidelityPlan(
        ref="calibration",
        project_ref="team-a",
        cells=tuple(cells),
        reserved_device_seconds=180,
        seed=42,
    )


def finish(service, job, cell, *, synthetic=False):
    # Hand-built envelopes exercise the validator only; published trials must
    # independently show a real backend, GPU allocation and runtime outputs.
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
        service.store.change_job(conn, row, State.COLLECTING, row["body"])
        b = row["body"]
    per_unit = (
        (2 if cell["arm"] == "cpu1" else 1)
        if cell["phase"] == "F1"
        else (1 if cell["arm"] == "cpu1" else 2)
    )
    result = ExecutionResult(
        job_id=job["job_id"],
        attempt_id=b["attempt_id"],
        epoch=1,
        workload_signature=b["workload_signature"],
        context_signature=b["context_signature"],
        evidence_kind="synthetic" if synthetic else "hardware",
        outcome="COMPLETED",
        measurements=Measurements(
            elapsed_seconds=per_unit * cell["work_units"],
            peak_memory_mib=100,
            quality_value=1,
            sample_count=cell["work_units"],
            work_units=cell["work_units"],
        ),
    )
    service.ingest("team-a", result, signature(result))


def test_preregistered_balanced_schedule_is_stable_and_sequential(service, bundle):
    plan = prepare(service, bundle)
    cal = FidelityCalibration(service)
    saved = cal.create("team-a", plan)
    assert [s["slot"] for s in saved["schedule"]] == list(range(18))
    for block in range(3):
        assert len({(s["arm"], s["phase"]) for s in saved["schedule"] if s["block"] == block}) == 6
    assert cal.create("team-a", plan) == saved
    with pytest.raises(Rejected):
        cal.submit_slot("team-a", plan.ref, 1)
    job = cal.submit_slot("team-a", plan.ref, 0)
    assert cal.submit_slot("team-a", plan.ref, 0) == job
    with pytest.raises(Rejected):
        cal.submit_slot("team-a", plan.ref, 1)
    finish(service, job, saved["schedule"][0])
    assert cal.submit_slot("team-a", plan.ref, 1)["job_id"] != job["job_id"]
    with service.store.transaction() as conn:
        assert len(conn.execute(select(jobs)).all()) == 2


@pytest.mark.parametrize("problem", ["shape", "consent", "budget"])
def test_calibration_rejects_uncontrolled_or_unfunded_comparison(service, bundle, problem):
    plan = prepare(
        service, bundle, mismatched_shape=problem == "shape", consent=problem != "consent"
    )
    if problem == "budget":
        plan = plan.model_copy(update={"reserved_device_seconds": 179})
    with pytest.raises(Rejected):
        FidelityCalibration(service).create("team-a", plan)


def test_complete_pairs_keep_independent_ids_detect_reversal_and_do_not_enable_mfkg(
    service, bundle
):
    plan = prepare(service, bundle)
    cal = FidelityCalibration(service)
    saved = cal.create("team-a", plan)
    assert not cal.assess("team-a", plan.ref)["complete"]
    for cell in saved["schedule"]:
        job = cal.submit_slot("team-a", plan.ref, cell["slot"])
        finish(service, job, cell)
    assessment = cal.assess("team-a", plan.ref)
    assert assessment["complete"]
    assert len({r["attempt_id"] for r in assessment["rows"]}) == 18
    assert "RANK_REVERSAL" in assessment["reasons"]
    assert "THERMAL_BEHAVIOR_UNVERIFIED" in assessment["reasons"]
    assert "REPLICATION_ONLY_NOT_MULTI_FIDELITY" in assessment["reasons"]
    assert not assessment["multi_fidelity_eligible"] and not assessment["early_pruning_allowed"]
    assert assessment["costs"][0]["unknown_allocation_attempts"] == 18
    assert assessment["costs"][0]["known_allocated_device_seconds"] == 0
    assert cal.assess("team-a", plan.ref) == assessment
    with service.store.transaction() as conn:
        assert len(service.store.list(conn, "fidelity_assessment", "team-a")) == 1


def test_synthetic_measurements_never_qualify_pairing(service, bundle):
    plan = prepare(service, bundle)
    cal = FidelityCalibration(service)
    saved = cal.create("team-a", plan)
    job = cal.submit_slot("team-a", plan.ref, 0)
    finish(service, job, saved["schedule"][0], synthetic=True)
    a = cal.assess("team-a", plan.ref)
    assert a["rows"][0]["seconds_per_work_unit"] is None
    assert "MISSING_OR_UNQUALIFIED_CELL" in a["reasons"]


def test_slot_key_cannot_relabel_another_workload(service, bundle):
    plan = prepare(service, bundle)
    cal = FidelityCalibration(service)
    cal.create("team-a", plan)
    service.submit(
        "team-a", JobRequest(workload_ref="workload-1", candidate_ref="base"), cal.key(plan.ref, 0)
    )
    with pytest.raises(Rejected):
        cal.submit_slot("team-a", plan.ref, 0)
    assert "SLOT_EXECUTION_CONTRACT_MISMATCH" in cal.assess("team-a", plan.ref)["reasons"]


def test_deadline_stops_new_slots_but_replay_is_stable(service, bundle, monkeypatch):
    plan = prepare(service, bundle)
    cal = FidelityCalibration(service)
    saved = cal.create("team-a", plan)
    job = cal.submit_slot("team-a", plan.ref, 0)
    finish(service, job, saved["schedule"][0])
    future = now() + timedelta(seconds=901)
    monkeypatch.setattr("resource_advisor.fidelity.now", lambda: future)
    assert cal.submit_slot("team-a", plan.ref, 0)["job_id"] == job["job_id"]
    with pytest.raises(Rejected, match="wall budget"):
        cal.submit_slot("team-a", plan.ref, 1)


def test_calibration_routes_require_operator_creation_and_hide_other_projects(service, bundle):
    plan = prepare(service, bundle)
    tokens = {
        hashlib.sha256(k.encode()).hexdigest(): v
        for k, v in {
            "op": Principal("team-a", True),
            "user": Principal("team-a"),
            "other": Principal("team-b"),
        }.items()
    }
    client = TestClient(create_app(service, tokens))
    path = "/api/v1/compute/fidelity-plans"
    assert client.post(path, json=plan.model_dump(mode="json")).status_code == 401
    assert (
        client.post(
            path, json=plan.model_dump(mode="json"), headers={"Authorization": "Bearer user"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            path, json=plan.model_dump(mode="json"), headers={"Authorization": "Bearer op"}
        ).status_code
        == 200
    )
    assert (
        client.get(path + "/" + plan.ref, headers={"Authorization": "Bearer other"}).status_code
        == 404
    )
    with pytest.raises(NotFound):
        FidelityCalibration(service).assess("team-b", plan.ref)
