"""Database-bound MF inputs. Hand-built result envelopes are not GPU evidence."""

import hashlib
from collections import Counter
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation
from resource_advisor.contracts import ProfilingPolicy, State, StudyRequest, now, signature
from resource_advisor.fidelity_space import FidelityEvidenceRequest, FidelitySpace, FidelitySpaces
from resource_advisor.mfkg import MFKernelInput, ask_mfkg
from resource_advisor.sampling import SamplingBindingRequest, SamplingPolicies, SamplingPolicy
from resource_advisor.search import device_unit
from resource_advisor.service import NotFound, Rejected
from resource_advisor.store import jobs
from resource_advisor.study import Studies
from resource_advisor.worker import Worker


@pytest.fixture
def space_fixture(service, bundle):
    original, base, variant, _ = bundle
    policy = SamplingPolicy(
        ref="sample-policy",
        project_ref="team-a",
        dataset_version=original.identity.dataset_version,
        input_shape=original.identity.input_shape,
        precision=original.identity.precision,
        batch_size=1,
        population=tuple(
            {
                "ref": f"sample-{i}",
                "stratum": f"class-{i % 2}",
                "content_digest": signature(["fixture-input", i]),
            }
            for i in range(20)
        ),
    )
    service.register("sampling_policy", policy, "team-a")
    for ref, units in [("full", 20), ("short", 10)]:
        identity = original.identity.model_copy(
            update={"work_units": units, "sampling_policy_digest": signature(policy)}
        )
        v = variant.model_copy(
            update={
                "ref": "v-" + ref,
                "workload_ref": ref,
                "workload_signature": signature(identity),
                "pilot_command": variant.command,
            }
        )
        candidates = tuple(
            base.model_copy(
                update={
                    "ref": name,
                    "variant_ref": v.ref,
                    "context": base.context.model_copy(
                        update={
                            "resources": base.context.resources.model_copy(update={"host_cpu": cpu})
                        }
                    ),
                }
            )
            for name, cpu in [("base", 1), ("other", 2)]
        )
        spec = original.model_copy(
            update={
                "ref": ref,
                "identity": identity,
                "candidates": candidates,
                "profiling": ProfilingPolicy(
                    consent=True,
                    max_probes=12,
                    mutable_parameters=("host_cpu",),
                    total_wall_seconds=3600,
                    device_seconds={device_unit(base, v): 3600},
                ),
            }
        )
        service.register("variant", v, "team-a")
        service.register("workload", spec, "team-a")
        SamplingPolicies(service).bind(
            "team-a", SamplingBindingRequest(workload_ref=ref, policy_ref=policy.ref)
        )
    request = FidelitySpace(
        ref="paired-space",
        project_ref="team-a",
        target_workload_ref="full",
        lower_workload_refs=("short",),
        feature_names=("host_cpu",),
        fidelity_axis="representative_sampling",
        sampling_policy_digest=signature(policy),
    )
    return service, FidelitySpaces(service), request


def with_sampling_fixture(job, result):
    """Explicit receipt validator fixture; this does not claim actual input consumption."""
    plan = job["body"]["sampling_binding"]["plan"]
    return {
        "result": result,
        "digest": signature(result),
        "sampling_receipt": {
            "job_id": job["id"],
            "attempt_id": job["body"]["attempt_id"],
            "result_digest": signature(result),
            "policy_digest": plan["policy_digest"],
            "selection_digest": plan["selection_digest"],
            "warmup_units": plan["warmup_units"],
            "measured_samples": [
                {"ref": s["ref"], "content_digest": s["content_digest"]} for s in plan["samples"]
            ],
        },
    }


def collect_test_envelopes(service, monkeypatch):
    """Existing submit/worker/ingest path, explicit scheduler and measurement doubles."""

    def balanced(_strategy, candidates, observations, _quality, _seed):
        counts = Counter(o["candidate_ref"] for o in observations)
        chosen = min(candidates, key=lambda c: counts[c.ref])
        return {"candidate_ref": chosen.ref, "reason": "TEST_DESIGN", "planning_seconds": 0}

    monkeypatch.setattr("resource_advisor.study.ask", balanced)
    backend = SchedulerDouble()
    backend.observation = Observation(State.COLLECTING)

    def result(job):
        envelope = SchedulerDouble.result(backend, job)
        r = envelope["result"]
        # Hardware-shaped validation fixture, not an observed hardware run.
        r["evidence_kind"] = "hardware"
        r["measurements"].update(
            work_units=job["body"]["spec"]["identity"]["work_units"],
            sample_count=job["body"]["spec"]["identity"]["work_units"],
            elapsed_seconds=0.0000001,
        )
        return with_sampling_fixture(job, r)

    backend.result = result
    worker = Worker(service, {("team-a", "lab"): backend})
    ids = []
    for ref in ["full", "short"]:
        engine = Studies(service)
        study = engine.create("team-a", StudyRequest(workload_ref=ref, strategy="random"), ref)
        for _ in range(30):
            engine.tick(study["ref"])
            worker.submit_one()
            worker.reconcile_all()
            current = engine.get("team-a", study["ref"])
            if len(current["observations"]) == 4:
                ids.extend(o["job_id"] for o in current["observations"])
                engine.cancel("team-a", study["ref"])
                engine.tick(study["ref"])
                break
        else:
            pytest.fail("four reserved probes were not observed")
    return ids


def test_immutable_space_derives_numeric_and_target_bindings_without_execution(space_fixture):
    service, spaces, request = space_fixture
    saved = spaces.create("team-a", request)
    assert spaces.create("team-a", request) == saved
    assert {(o["coordinates"][0], o["fidelity"]) for o in saved["options"]} == {
        (0, 0.5),
        (1, 0.5),
        (0, 1),
        (1, 1),
    }
    low = next(o for o in saved["options"] if o["fidelity"] < 1)
    preview = spaces.resolve("team-a", request.ref, low["ref"])
    assert preview["measurement_binding"]["workload_ref"] == "short"
    assert preview["independent_confirmation_binding"]["workload_ref"] == "full"
    assert not preview["execution_authorized"]
    with service.store.transaction() as conn:
        assert not conn.execute(select(jobs)).all()
        assert not service.store.list(conn, "probe_plan", "team-a")
    with pytest.raises(Rejected, match="paired-fidelity"):
        Studies(service).create(
            "team-a", StudyRequest(workload_ref="full", strategy="mfkg"), "blocked"
        )


@pytest.mark.parametrize(
    "change", ["input_shape", "dataset_version", "precision", "seed", "config_digest"]
)
def test_rejects_different_logical_workload(space_fixture, change):
    service, spaces, request = space_fixture
    # Corrupt the persisted fixture to simulate independently registered conflicting contracts.
    from resource_advisor.store import entities

    with service.store.transaction() as conn:
        row = service.store.get(conn, "workload", "short")
        body = row["body"]
        replacements = {
            "input_shape": [1, 3, 64, 64],
            "dataset_version": "other",
            "precision": "fp16",
            "seed": 10,
            "config_digest": signature("other"),
        }
        body["identity"][change] = replacements[change]
        conn.execute(
            update(entities)
            .where(entities.c.kind == "workload", entities.c.ref == "short")
            .values(body=body)
        )
    with pytest.raises(Rejected):
        spaces.create("team-a", request)


def test_changed_config_and_invalid_features_are_rejected(space_fixture):
    _, spaces, request = space_fixture
    with pytest.raises(Rejected, match="vary"):
        spaces.create("team-a", request.model_copy(update={"feature_names": ("host_memory_mib",)}))
    with pytest.raises(ValidationError):
        spaces.create("team-a", request.model_copy(update={"lower_workload_refs": ("full",)}))
    with pytest.raises(ValidationError):
        spaces.create("team-a", request.model_copy(update={"sampling_policy_digest": None}))


def test_evidence_joins_jobs_results_and_real_kernel_but_cannot_authorize(
    space_fixture, monkeypatch
):
    pytest.importorskip("botorch")
    service, spaces, request = space_fixture
    saved = spaces.create("team-a", request)
    ids = collect_test_envelopes(service, monkeypatch)
    report = spaces.evidence("team-a", request.ref, FidelityEvidenceRequest(job_ids=tuple(ids)))
    assert (
        spaces.evidence(
            "team-a", request.ref, FidelityEvidenceRequest(job_ids=tuple(reversed(ids)))
        )
        == report
    )
    assert len(report["provenance"]) == 8
    problem = MFKernelInput.model_validate(report["kernel_input"])
    assert all(
        o.runtime_group_signature == saved["runtime_group_signature"] for o in problem.observations
    )
    answer = ask_mfkg(problem)
    assert len(answer["surrogate"]["training_run_ids"]) == 8
    assert not answer["execution_authorized"] and not report["execution_authorized"]
    assert report["sampling_policy_verified"] and report["sampling_receipts_verified"]
    assert report["qualification_reasons"] == ["PAIRED_RANK_AND_THERMAL_QUALIFICATION_REQUIRED"]
    with service.store.transaction() as conn:
        assert len(service.store.list(conn, "mf_evidence", "team-a")) == 1
        assert not service.store.list(
            conn, "profile", "team-a"
        )  # pilots never enter target history


def test_replication_space_never_exports_mf_kernel_input(space_fixture, monkeypatch):
    service, spaces, request = space_fixture
    request = request.model_copy(
        update={"fidelity_axis": "identical_input_repetition", "sampling_policy_digest": None}
    )
    spaces.create("team-a", request)
    ids = collect_test_envelopes(service, monkeypatch)
    report = spaces.evidence("team-a", request.ref, FidelityEvidenceRequest(job_ids=tuple(ids)))
    assert report["kernel_input"] is None
    assert report["qualification_reasons"] == ["REPLICATION_ONLY_NOT_MULTI_FIDELITY"]


@pytest.mark.parametrize("fault", ["missing", "altered", "snapshot"])
def test_sampling_evidence_requires_immutable_consumption_receipts(
    space_fixture, monkeypatch, fault
):
    from resource_advisor.store import entities

    service, spaces, request = space_fixture
    spaces.create("team-a", request)
    ids = collect_test_envelopes(service, monkeypatch)
    with service.store.transaction() as conn:
        row = service.store.job(conn, ids[0])
        attempt = row["body"]["attempt_id"]
        if fault == "missing":
            conn.execute(
                delete(entities).where(
                    entities.c.kind == "sampling_receipt", entities.c.ref == attempt
                )
            )
        elif fault == "altered":
            receipt = service.store.get(conn, "sampling_receipt", attempt)["body"]
            receipt["measured_samples"].reverse()
            conn.execute(
                update(entities)
                .where(entities.c.kind == "sampling_receipt", entities.c.ref == attempt)
                .values(body=receipt)
            )
        else:
            body = row["body"]
            body["sampling_binding"]["plan"]["seed"] += 1
            service.store.change_job(conn, row, row["state"], body)
    with pytest.raises(Rejected, match="sampling receipt"):
        spaces.evidence("team-a", request.ref, FidelityEvidenceRequest(job_ids=tuple(ids)))


def test_representative_space_requires_registered_policy_digest(space_fixture):
    _, spaces, request = space_fixture
    with pytest.raises(Rejected, match="registered sampling policy"):
        spaces.create(
            "team-a", request.model_copy(update={"sampling_policy_digest": signature("different")})
        )


@pytest.mark.parametrize(
    "problem",
    ["confirmation", "synthetic", "digest", "outside", "stale", "epoch", "memory", "cost"],
)
def test_untrusted_incomplete_or_leaking_evidence_is_rejected(space_fixture, monkeypatch, problem):
    from resource_advisor.store import entities

    service, spaces, request = space_fixture
    spaces.create("team-a", request)
    ids = collect_test_envelopes(service, monkeypatch)
    with service.store.transaction() as conn:
        row = service.store.job(conn, ids[0])
        body = row["body"]
        if problem == "confirmation":
            body["request"]["mode"] = "confirmation"
        elif problem == "digest":
            body["result_digest"] = signature("bad")
        elif problem == "outside":
            body["spec"]["ref"] = "elsewhere"
        elif problem == "stale":
            body["finished_at"] = (now() - timedelta(days=2)).isoformat()
        elif problem == "cost":
            body["created_at"] = body["finished_at"]
        else:
            result = service.store.get(conn, "result", body["attempt_id"])["body"]
            if problem == "synthetic":
                result["evidence_kind"] = "synthetic"
            elif problem == "epoch":
                result["epoch"] += 1
            else:
                result["measurements"]["peak_memory_mib"] = 100000
            body["result_digest"] = signature(result)
            conn.execute(
                update(entities)
                .where(entities.c.kind == "result", entities.c.ref == body["attempt_id"])
                .values(body=result)
            )
        service.store.change_job(conn, row, row["state"], body)
    with pytest.raises(Rejected):
        spaces.evidence("team-a", request.ref, FidelityEvidenceRequest(job_ids=tuple(ids)))


def test_current_context_rechecked_but_historical_evidence_survives_capability_expiry(
    space_fixture, monkeypatch
):
    service, spaces, request = space_fixture
    saved = spaces.create("team-a", request)
    ids = collect_test_envelopes(service, monkeypatch)
    future = now() + timedelta(seconds=600)
    monkeypatch.setattr("resource_advisor.policy.now", lambda: future)
    with pytest.raises(Rejected, match="no longer compatible"):
        spaces.resolve("team-a", request.ref, saved["options"][0]["ref"])
    assert spaces.evidence("team-a", request.ref, FidelityEvidenceRequest(job_ids=tuple(ids)))[
        "kernel_input"
    ]


def test_routes_operator_scope_and_no_posted_measurements(space_fixture):
    service, spaces, request = space_fixture
    credentials = {
        hashlib.sha256(k.encode()).hexdigest(): p
        for k, p in {
            "op": Principal("team-a", True),
            "user": Principal("team-a"),
            "other": Principal("team-b"),
        }.items()
    }
    client = TestClient(create_app(service, credentials))
    path = "/api/v1/compute/fidelity-spaces"

    def headers(key):
        return {"Authorization": "Bearer " + key}

    assert client.post(path, json=request.model_dump(mode="json")).status_code == 401
    assert (
        client.post(path, json=request.model_dump(mode="json"), headers=headers("user")).status_code
        == 403
    )
    response = client.post(path, json=request.model_dump(mode="json"), headers=headers("op"))
    assert response.status_code == 200
    assert client.get(path + "/" + request.ref, headers=headers("other")).status_code == 404
    assert (
        client.post(
            path + "/" + request.ref + "/evidence",
            json={"job_ids": ["a"] * 8, "seconds": 0.1},
            headers=headers("user"),
        ).status_code
        == 422
    )
    with pytest.raises(NotFound):
        spaces.get("team-b", request.ref)


class FidelitySchedulerDouble(SchedulerDouble):
    """Control-flow fixture only; no physical device is used."""

    def result(self, job):
        envelope = super().result(job)
        value = envelope["result"]
        units = job["body"]["spec"]["identity"]["work_units"]
        base = job["body"]["candidate"]["ref"] == "base"
        # Deliberate low/target rank reversal tests target-only confirmation.
        seconds = (1e-7 if base else 1e-5) if units == 10 else (3e-7 if base else 2e-7)
        value["measurements"].update(work_units=units, sample_count=units, elapsed_seconds=seconds)
        value["evidence_kind"] = "hardware"  # validator fixture, not hardware evidence
        return with_sampling_fixture(job, value)


def start_calibration(space_fixture):
    service, spaces, request = space_fixture
    saved = spaces.create("team-a", request)
    command = StudyRequest(
        workload_ref="full", strategy="fidelity_calibration", fidelity_space_ref=request.ref, seed=7
    )
    study = Studies(service).create("team-a", command, "calibration")
    assert Studies(service).create("team-a", command, "calibration")["ref"] == study["ref"]
    backend = FidelitySchedulerDouble()
    backend.observation = Observation(State.COLLECTING)
    return service, spaces, saved, study, Worker(service, {("team-a", "lab"): backend}), backend


def test_mixed_fidelity_durable_execution_and_independent_target_confirmation(space_fixture):
    service, spaces, saved, study, worker, _ = start_calibration(space_fixture)
    for block in range(3):
        assert (
            len(
                {
                    slot["option_ref"]
                    for slot in study["fidelity_schedule"]
                    if slot["block"] == block
                }
            )
            == 4
        )
    for _ in range(100):
        Studies(service).tick(study["ref"])  # reconstruct coordinator at every boundary
        worker.submit_one()
        worker.reconcile_all()
        current = Studies(service).get("team-a", study["ref"])
        if current["state"] in {"COMPLETED", "ABSTAINED"}:
            break
    assert current["state"] == "COMPLETED", current
    probes = [o for o in current["observations"] if o["mode"] == "pilot"]
    confirms = [o for o in current["observations"] if o["mode"] == "confirmation"]
    assert len(probes) == 12 and len(confirms) == 6
    assert Counter(o["option_ref"] for o in probes) == {o["ref"]: 3 for o in saved["options"]}
    assert {o["workload_ref"] for o in probes} == {"full", "short"}
    assert {o["workload_ref"] for o in confirms} == {"full"}
    assert {o["attempt_id"] for o in probes}.isdisjoint(o["attempt_id"] for o in confirms)
    assert current["predicted_candidate"] == "other"  # low-rank reversal cannot select the finalist
    assert current["recommendation"]["confirmed_candidate"] == "other"
    assert current["recommendation"]["workload_ref"] == "full"
    assert current["charged_device_seconds"]
    with service.store.transaction() as conn:
        profiles = service.store.list(conn, "profile", "team-a")
        assert len(profiles) == 6
        assert {p["body"]["result"]["measurements"]["work_units"] for p in profiles} == {20}
        for ref in current["plans"]:
            plan = service.store.get(conn, "probe_plan", ref)["body"]
            spec = service.store.get(conn, "workload", plan["workload_ref"])["body"]
            assert plan["workload_digest"] == signature(spec)
            assert plan["fidelity_space_digest"] == signature(saved)
    report = spaces.evidence(
        "team-a",
        saved["request"]["ref"],
        FidelityEvidenceRequest(job_ids=tuple(o["job_id"] for o in probes)),
    )
    assert len(report["observations"]) == 12 and not report["execution_authorized"]
    omitted = probes[0]["option_ref"]
    with pytest.raises(Rejected, match="incomplete"):
        spaces.evidence(
            "team-a",
            saved["request"]["ref"],
            FidelityEvidenceRequest(
                job_ids=tuple(o["job_id"] for o in probes if o["option_ref"] != omitted)
            ),
        )
    # Final confirmation remains held out even when the caller explicitly requests it.
    with pytest.raises(Rejected, match="confirmation"):
        spaces.evidence(
            "team-a",
            saved["request"]["ref"],
            FidelityEvidenceRequest(job_ids=tuple(o["job_id"] for o in probes + confirms)),
        )


def test_mixed_plan_cannot_submit_target_in_place_of_lower_workload(space_fixture):
    from resource_advisor.contracts import JobRequest

    service, _, saved, study, _, _ = start_calibration(space_fixture)
    Studies(service).tick(study["ref"])
    current = Studies(service).get("team-a", study["ref"])
    with service.store.transaction() as conn:
        plan = service.store.get(conn, "probe_plan", current["active_plan"])["body"]
    wrong = "short" if plan["workload_ref"] == "full" else "full"
    with pytest.raises(Rejected, match="reservation"):
        service.submit(
            "team-a",
            JobRequest(
                workload_ref=wrong,
                candidate_ref=plan["candidate_ref"],
                mode="pilot",
                study_ref=study["ref"],
                probe_plan_ref=plan["ref"],
            ),
            "probe-" + plan["ref"],
        )
    with service.store.transaction() as conn:
        assert not conn.execute(select(jobs)).all()


@pytest.mark.parametrize("stop", ["cancel", "stale", "deadline", "failure"])
def test_mixed_calibration_stops_without_unapproved_followup(space_fixture, monkeypatch, stop):
    service, _, _, study, worker, backend = start_calibration(space_fixture)
    engine = Studies(service)
    if stop == "stale":
        future = now() + timedelta(seconds=600)
        monkeypatch.setattr("resource_advisor.policy.now", lambda: future)
    elif stop == "cancel":
        engine.tick(study["ref"])
        engine.cancel("team-a", study["ref"])
    elif stop == "deadline":
        with service.store.transaction() as conn:
            row = service.store.study(conn, study["ref"])
            service.store.change_study(
                conn,
                row,
                row["state"],
                dict(row["body"], deadline_at=(now() - timedelta(seconds=1)).isoformat()),
            )
    else:
        backend.observation = Observation(State.FAILED, error="OUT_OF_MEMORY")
        for _ in range(3):
            engine.tick(study["ref"])
            worker.submit_one()
            worker.reconcile_all()
    engine.tick(study["ref"])
    current = engine.get("team-a", study["ref"])
    assert current["state"] == ("CANCELED" if stop == "cancel" else "ABSTAINED"), current
    assert backend.submissions == (1 if stop == "failure" else 0)
    assert current["recommendation_ref"] is None


def test_legacy_study_request_digest_does_not_change(space_fixture):
    service, _, _ = space_fixture
    request = StudyRequest(workload_ref="full", strategy="random")
    study = Studies(service).create("team-a", request, "legacy-digest")
    with service.store.transaction() as conn:
        row = service.store.study(conn, study["ref"])
    assert row["request_digest"] == signature(
        {"workload_ref": "full", "strategy": "random", "seed": 0}
    )


def test_canceled_planning_lease_cannot_create_calibration_plan(space_fixture):
    from resource_advisor.store import Conflict

    service, _, _, study, _, _ = start_calibration(space_fixture)
    with service.store.transaction() as conn:
        row = service.store.study(conn, study["ref"])
        service.store.change_study(conn, row, "PLANNING", dict(row["body"], planning_token="old"))
    Studies(service).cancel("team-a", study["ref"])
    with pytest.raises(Conflict, match="lease"):
        Studies(service)._plan(study["ref"], "old")
    with service.store.transaction() as conn:
        assert not service.store.list(conn, "probe_plan", "team-a")
