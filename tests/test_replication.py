import pytest
import test_study
from pydantic import ValidationError
from sqlalchemy import select, update
from test_worker import SchedulerDouble

from resource_advisor.backends import Observation
from resource_advisor.contracts import ReplicationPolicy, State, StudyRequest, signature
from resource_advisor.replication import ask_replication
from resource_advisor.service import Rejected
from resource_advisor.store import studies
from resource_advisor.study import Studies
from resource_advisor.worker import Worker

study_fixture = test_study.study_fixture


def observations(ref, values):
    return [
        {
            "candidate_ref": ref,
            "attempt_id": f"{ref}-{i}",
            "outcome": "COMPLETED",
            "measurements": {
                "elapsed_seconds": value,
                "quality_value": 1,
                "peak_memory_mib": 100,
                "sample_count": 1000000,
            },
        }
        for i, value in enumerate(values)
    ]


def test_inner_samples_do_not_replace_independent_runs(bundle):
    spec, base, _, _ = bundle
    policy = ReplicationPolicy()
    result = ask_replication([base], observations(base.ref, [1]), spec.quality, policy, 4)
    assert result["statistics"][0]["independent_runs"] == 1
    assert result["statistics"][0]["relative_standard_error"] is None
    assert not result["stop_exploration"]
    duplicated = observations(base.ref, [1]) * 3
    with pytest.raises(ValueError, match="unique independent"):
        ask_replication([base], duplicated, spec.quality, policy, 4)


def test_noise_directs_additional_measurement_not_faster_mean(study_fixture):
    _, _, spec = study_fixture
    runs = observations("base", [1, 1, 1]) + observations("other", [2, 10, 3])
    answer = ask_replication(spec.candidates, runs, spec.quality, ReplicationPolicy(), 4)
    assert answer["candidate_ref"] == "other"
    assert answer["reason"] == "REPLICATION_LARGEST_ESTIMATED_VARIANCE_REDUCTION"
    assert answer["multi_fidelity"] is False


def test_balanced_seeded_initial_design(study_fixture):
    _, _, spec = study_fixture
    runs = observations("base", [1, 1]) + observations("other", [2])
    answer = ask_replication(spec.candidates, runs, spec.quality, ReplicationPolicy(), 7)
    assert answer["candidate_ref"] == "other"
    assert answer["reason"] == "REPLICATION_BALANCED_INITIAL_RUNS"
    again = ask_replication(
        tuple(reversed(spec.candidates)), runs, spec.quality, ReplicationPolicy(), 7
    )
    assert again["candidate_ref"] == answer["candidate_ref"]


def test_noisy_limit_and_precision_are_distinct(bundle):
    spec, base, _, _ = bundle
    policy = ReplicationPolicy(minimum_runs=3, maximum_runs=3)
    noisy = ask_replication([base], observations(base.ref, [1, 10, 3]), spec.quality, policy, 0)
    stable = ask_replication([base], observations(base.ref, [2, 2, 2]), spec.quality, policy, 0)
    assert noisy["stop_exploration"] and stable["stop_exploration"]
    assert noisy["reason"] == "REPLICATION_PER_CANDIDATE_LIMIT"
    assert stable["reason"] == "REPLICATION_PRECISION_TARGET_MET"
    assert not noisy["statistics"][0]["target_met"]


@pytest.mark.parametrize("failure", ["OOM", "QUALITY", "MEMORY", "NAN"])
def test_failed_repeat_never_becomes_fast_measurement(bundle, failure):
    spec, base, _, _ = bundle
    runs = observations(base.ref, [2, 2, 0.01])
    if failure == "OOM":
        runs[-1].update(outcome="OOM", measurements=None)
    else:
        key, value = {
            "QUALITY": ("quality_value", 0),
            "MEMORY": ("peak_memory_mib", 100000),
            "NAN": ("elapsed_seconds", float("nan")),
        }[failure]
        runs[-1]["measurements"][key] = value
    result = ask_replication([base], runs, spec.quality, ReplicationPolicy(), 0)
    assert result["candidate_ref"] is None
    assert result["reason"] == "REPLICATION_NO_FEASIBLE_CANDIDATE"
    assert base.ref in result["excluded"]


def test_options_and_initial_budget_are_explicit(study_fixture):
    _, engine, spec = study_fixture
    with pytest.raises(ValidationError):
        StudyRequest(workload_ref=spec.ref, strategy="adaptive_replication")
    with pytest.raises(ValidationError):
        StudyRequest(workload_ref=spec.ref, strategy="random", replication=ReplicationPolicy())
    with pytest.raises(ValidationError):
        ReplicationPolicy(minimum_runs=5, maximum_runs=3)
    with pytest.raises(Rejected, match="initial design"):
        engine.create(
            "team-a",
            StudyRequest(
                workload_ref=spec.ref,
                strategy="adaptive_replication",
                replication=ReplicationPolicy(),
            ),
            "insufficient-budget",
        )


def registered_spec(service, spec, max_probes=12):
    with service.store.transaction() as conn:
        variant = dict(service.store.get(conn, "variant", spec.candidates[0].variant_ref)["body"])
    variant.update(ref="adaptive-variant", workload_ref="adaptive-workload")
    with service.store.transaction() as conn:
        service.store.put(conn, "variant", variant["ref"], "team-a", variant)
    spec = spec.model_copy(
        update={
            "ref": variant["workload_ref"],
            "candidates": tuple(
                c.model_copy(update={"variant_ref": variant["ref"]}) for c in spec.candidates
            ),
            "profiling": spec.profiling.model_copy(update={"max_probes": max_probes}),
        }
    )
    service.register("workload", spec, "team-a")
    return spec


@pytest.mark.parametrize("noisy", [False, True])
def test_real_coordinator_adapts_then_confirms_independent_jobs(study_fixture, noisy):
    # Actual service/DB/coordinator with a synthetic scheduler, not GPU evidence.
    service, engine, spec = study_fixture
    spec = registered_spec(service, spec)
    req = StudyRequest(
        workload_ref=spec.ref,
        strategy="adaptive_replication",
        replication=ReplicationPolicy(maximum_runs=4),
        seed=17,
    )
    study = engine.create("team-a", req, "replicate")
    assert engine.create("team-a", req, "replicate")["ref"] == study["ref"]
    backend = SchedulerDouble()
    original_result = backend.result
    seen = {}

    def result(job):
        envelope = original_result(job)
        request = job["body"]["request"]
        if noisy and request["candidate_ref"] == "other" and request["mode"] == "pilot":
            index = seen.setdefault(job["id"], len(seen))
            envelope["result"]["measurements"]["elapsed_seconds"] = [1.0, 3.0, 1.0, 3.0][index]
            envelope["digest"] = signature(envelope["result"])
        return envelope

    backend.result = result
    backend.observation = Observation(State.COLLECTING)
    worker = Worker(service, {("team-a", "lab"): backend})
    for _ in range(80):
        Studies(service).tick(study["ref"])
        worker.submit_one()
        worker.reconcile_all()
        current = engine.get("team-a", study["ref"])
        if current["state"] in {"COMPLETED", "ABSTAINED"}:
            break
    assert current["state"] == "COMPLETED", current
    pilots = [o for o in current["observations"] if o["mode"] == "pilot"]
    confirms = [o for o in current["observations"] if o["mode"] == "confirmation"]
    assert len(pilots) == (7 if noisy else 6)
    assert len(pilots) < spec.profiling.max_probes
    assert len(confirms) >= spec.quality.minimum_repeats
    assert {o["attempt_id"] for o in pilots}.isdisjoint(o["attempt_id"] for o in confirms)
    assert current["exploration_stop_reason"] == (
        "REPLICATION_PER_CANDIDATE_LIMIT" if noisy else "REPLICATION_PRECISION_TARGET_MET"
    )
    assert current["recommendation"]["candidate_ref"] == "base"
    assert backend.submissions == len(pilots) + len(confirms)
    assert all(
        o["cost_source"] == "conservative_reservation_unknown_actual"
        for o in current["observations"]
    )


def test_expired_capability_stops_replication_instead_of_shrinking_space(study_fixture):
    service, engine, spec = study_fixture
    spec = registered_spec(service, spec)
    study = engine.create(
        "team-a",
        StudyRequest(
            workload_ref=spec.ref, strategy="adaptive_replication", replication=ReplicationPolicy()
        ),
        "context-drift",
    )
    from datetime import timedelta

    from resource_advisor.contracts import now
    from resource_advisor.store import entities

    with service.store.transaction() as conn:
        cap = service.store.get(conn, "capability", spec.candidates[0].capability_ref)
        body = dict(cap["body"], observed_at=(now() - timedelta(days=1)).isoformat())
        conn.execute(
            update(entities)
            .where(entities.c.kind == "capability", entities.c.ref == cap["ref"])
            .values(body=body)
        )
    engine.tick(study["ref"])
    current = engine.get("team-a", study["ref"])
    assert current["state"] == "ABSTAINED"
    assert current["stop_reason"] == "REPLICATION_EXECUTION_CONTEXT_CHANGED"
    assert current["plans"] == []


def test_legacy_study_idempotency_digest_survives_new_options(study_fixture):
    service, engine, spec = study_fixture
    request = StudyRequest(workload_ref=spec.ref, strategy="random", seed=1)
    created = engine.create("team-a", request, "legacy")
    with service.store.transaction() as conn:
        conn.execute(
            update(studies)
            .where(studies.c.id == created["ref"])
            .values(
                request_digest=signature(
                    {"workload_ref": spec.ref, "strategy": "random", "seed": 1}
                )
            )
        )
    assert engine.create("team-a", request, "legacy")["ref"] == created["ref"]
    with service.store.transaction() as conn:
        assert len(conn.execute(select(studies)).all()) == 1
