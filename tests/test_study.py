from datetime import timedelta

import pytest
from sqlalchemy import select
from test_worker import SchedulerDouble

from resource_advisor.backends import Observation
from resource_advisor.contracts import JobRequest, ProfilingPolicy, State, StudyRequest, now
from resource_advisor.search import ask, device_unit, features
from resource_advisor.service import Rejected, Service
from resource_advisor.store import jobs
from resource_advisor.study import Studies
from resource_advisor.worker import Worker


@pytest.fixture
def study_fixture(bundle, database_store):
    spec, base, variant, cap = bundle
    other = base.model_copy(
        update={
            "ref": "other",
            "context": base.context.model_copy(
                update={"resources": base.context.resources.model_copy(update={"host_cpu": 2})}
            ),
        }
    )
    variant = variant.model_copy(update={"pilot_command": ("python", "pilot.py")})
    cap = cap.model_copy(update={"valid_for_seconds": 3600})
    policy = ProfilingPolicy(
        consent=True,
        max_probes=4,
        total_wall_seconds=3600,
        max_wall_seconds_per_candidate=20,
        final_validation_seconds=180,
        mutable_parameters=("host_cpu",),
        device_seconds={device_unit(base, variant): 3600},
    )
    spec = spec.model_copy(update={"candidates": (base, other), "profiling": policy})
    store = database_store
    service = Service(store, accept_synthetic=True)
    for kind, value in [("workload", spec), ("variant", variant), ("capability", cap)]:
        service.register(kind, value, "team-a")
    return service, Studies(service), spec


def test_no_consent_means_no_study(service):
    with pytest.raises(Rejected, match="consent"):
        Studies(service).create(
            "team-a", StudyRequest(workload_ref="workload-1", strategy="random"), "one"
        )


@pytest.mark.parametrize("strategy", ["random", "qlognei"])
def test_durable_loop_has_independent_confirmation(study_fixture, strategy):
    if strategy == "qlognei":
        pytest.importorskip("botorch")
    service, engine, spec = study_fixture
    request = StudyRequest(workload_ref=spec.ref, strategy=strategy, seed=5)
    study = engine.create("team-a", request, "one")
    assert engine.create("team-a", request, "one")["ref"] == study["ref"]
    backend = SchedulerDouble()
    backend.observation = Observation(State.COLLECTING)
    worker = Worker(service, {("team-a", "lab"): backend})
    for _ in range(60):
        Studies(service).tick(study["ref"])  # reconstruct coordinator, preserving durable state
        worker.submit_one()
        worker.reconcile_all()
        current = engine.get("team-a", study["ref"])
        if current["state"] in {"COMPLETED", "ABSTAINED"}:
            break
    assert current["state"] == "COMPLETED", current
    probes = [o for o in current["observations"] if o["mode"] == "pilot"]
    confirmations = [o for o in current["observations"] if o["mode"] == "confirmation"]
    assert len(probes) == 4
    assert len(confirmations) >= spec.quality.minimum_repeats
    assert {o["attempt_id"] for o in probes}.isdisjoint(o["attempt_id"] for o in confirmations)
    assert current["recommendation"]["confirmed_candidate"] == "base"
    with service.store.transaction() as conn:
        profiles = service.store.list(conn, "profile", "team-a")
        assert len(profiles) == len(confirmations)
        for row in conn.execute(select(jobs)).mappings():
            command = row["body"]["effective_command"]
            assert command == (
                ["python", "pilot.py"]
                if row["body"]["request"]["mode"] == "pilot"
                else ["python", "workload.py"]
            )
        assert all(
            c["cost_source"] == "conservative_reservation_unknown_actual"
            for c in current["observations"]
        )
        if strategy == "qlognei":
            plans = [service.store.get(conn, "probe_plan", ref)["body"] for ref in current["plans"]]
            assert any(p["choice"]["reason"] == "CONSTRAINED_QLOGNEI" for p in plans)


def test_cannot_forge_unreserved_probe(study_fixture):
    service, engine, spec = study_fixture
    study = engine.create("team-a", StudyRequest(workload_ref=spec.ref, strategy="random"), "one")
    with pytest.raises(Rejected):
        service.submit(
            "team-a",
            JobRequest(
                workload_ref=spec.ref, candidate_ref="base", mode="pilot", study_ref=study["ref"]
            ),
            "forged",
        )


def test_confirmation_reserves_time_for_scheduler_admission(study_fixture):
    service, engine, spec = study_fixture
    with service.store.transaction() as conn:
        variant = dict(service.store.get(conn, "variant", spec.candidates[0].variant_ref)["body"])
    variant.update(ref="confirmation-variant", workload_ref="confirmation-queue-budget")
    with service.store.transaction() as conn:
        service.store.put(conn, "variant", variant["ref"], "team-a", variant)
    spec = spec.model_copy(
        update={
            "ref": "confirmation-queue-budget",
            "candidates": tuple(
                c.model_copy(update={"variant_ref": variant["ref"]}) for c in spec.candidates
            ),
            "profiling": spec.profiling.model_copy(
                update={"max_wall_seconds_per_candidate": 60, "max_probes": 1}
            ),
        }
    )
    service.register("workload", spec, "team-a")
    study = engine.create("team-a", StudyRequest(workload_ref=spec.ref, strategy="random"), "queue")
    backend = SchedulerDouble()
    backend.observation = Observation(State.COLLECTING)
    worker = Worker(service, {("team-a", "lab"): backend})
    for _ in range(12):
        engine.tick(study["ref"])
        current = engine.get("team-a", study["ref"])
        if current["state"] == "CONFIRMING" and current["active_plan"]:
            with service.store.transaction() as conn:
                plan = service.store.get(conn, "probe_plan", current["active_plan"])["body"]
            limits = plan["execution_limits"]
            # With 180 s reserved and 3–6 confirmations, startup gets a usable
            # share without borrowing from another confirmation's budget.
            assert limits["max_queue_seconds"] >= 10
            assert limits["max_run_seconds"] >= 10
            assert sum(limits.values()) <= 180 / len(current["confirmation_schedule"])
            return
        worker.submit_one()
        worker.reconcile_all()
    pytest.fail("independent confirmation was not planned")


def test_reserved_plan_has_deadline_and_cannot_duplicate_with_another_key(study_fixture):
    service, engine, spec = study_fixture
    study = engine.create("team-a", StudyRequest(workload_ref=spec.ref, strategy="random"), "one")
    engine.tick(study["ref"])
    current = engine.get("team-a", study["ref"])
    with service.store.transaction() as conn:
        plan = service.store.get(conn, "probe_plan", current["active_plan"])["body"]
    assert (
        plan["execution_limits"]["max_run_seconds"] <= spec.profiling.max_wall_seconds_per_candidate
    )
    assert (
        plan["reserved_device_seconds"]
        <= spec.profiling.device_seconds[plan["device_unit"]]
        - spec.profiling.final_validation_seconds
    )
    with pytest.raises(Rejected, match="reservation"):
        service.submit(
            "team-a",
            JobRequest(
                workload_ref=spec.ref,
                candidate_ref=plan["candidate_ref"],
                mode="pilot",
                study_ref=study["ref"],
                probe_plan_ref=plan["ref"],
            ),
            "different-key",
        )


def test_cancel_before_dispatch_never_executes(study_fixture):
    service, engine, spec = study_fixture
    study = engine.create("team-a", StudyRequest(workload_ref=spec.ref, strategy="random"), "one")
    engine.tick(study["ref"])
    engine.cancel("team-a", study["ref"])
    engine.tick(study["ref"])
    assert engine.get("team-a", study["ref"])["state"] == "CANCELED"
    with service.store.transaction() as conn:
        assert not conn.execute(select(jobs)).all()


@pytest.mark.parametrize(
    "strategy,reason", [("mfkg", "paired-fidelity"), ("rgpe", "source models")]
)
def test_unqualified_advanced_strategy_is_not_disguised(study_fixture, strategy, reason):
    _, engine, spec = study_fixture
    with pytest.raises(Rejected, match=reason):
        engine.create("team-a", StudyRequest(workload_ref=spec.ref, strategy=strategy), "one")


def test_expired_study_cannot_submit(study_fixture):
    service, engine, spec = study_fixture
    study = engine.create("team-a", StudyRequest(workload_ref=spec.ref, strategy="random"), "one")
    with service.store.transaction() as conn:
        row = service.store.study(conn, study["ref"])
        service.store.change_study(
            conn,
            row,
            row["state"],
            dict(row["body"], deadline_at=(now() - timedelta(seconds=1)).isoformat()),
        )
    engine.tick(study["ref"])
    assert engine.get("team-a", study["ref"])["state"] == "ABSTAINED"


def test_features_do_not_order_device_names(bundle):
    base = bundle[1]
    other = base.model_copy(
        update={
            "ref": "other",
            "context": base.context.model_copy(update={"accelerator_model": "another-gpu"}),
        }
    )
    rows, schema = features([base, other])
    assert len(schema["runtime_categories"]) == 2
    assert sum(rows[0][-2:]) == sum(rows[1][-2:]) == 1
    assert rows[0][-2:] != rows[1][-2:]


def test_actual_botorch_acquisition_has_separate_predictions(study_fixture):
    pytest.importorskip("botorch")
    _, _, spec = study_fixture
    observations = [
        {
            "candidate_ref": c.ref,
            "attempt_id": f"run-{i}-{j}",
            "outcome": "COMPLETED",
            "measurements": {
                "elapsed_seconds": 10 - i * 3 + j * 0.1,
                "peak_memory_mib": 100 + i * 20,
                "quality_value": 0.99,
            },
        }
        for i, c in enumerate(spec.candidates)
        for j in range(2)
    ]
    result = ask("qlognei", list(spec.candidates), observations, spec.quality, 1)
    assert result["reason"] == "CONSTRAINED_QLOGNEI", result
    assert result["candidate_ref"] in {c.ref for c in spec.candidates}
    assert all(p["measured"] is False for p in result["predictions"])
    assert len(result["surrogate"]["training_run_ids"]) == 4


def test_oom_is_never_learned_as_fast_success(bundle):
    spec, base, _, _ = bundle
    result = ask(
        "qlognei",
        [base],
        [{"candidate_ref": base.ref, "outcome": "OOM", "measurements": None}],
        spec.quality,
        0,
    )
    assert result["candidate_ref"] is None
