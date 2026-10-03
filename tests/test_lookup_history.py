"""Synthetic evidence cohorts; none of these fixtures qualify hardware."""

import copy
from datetime import timedelta

import pytest
import test_study
from sqlalchemy import delete, select, update
from test_service import complete
from test_worker import SchedulerDouble

from resource_advisor.backends import Observation
from resource_advisor.contracts import State, StudyRequest, now, signature
from resource_advisor.service import NotFound, Rejected
from resource_advisor.store import entities, jobs, studies
from resource_advisor.study import Studies
from resource_advisor.worker import Worker

study_fixture = test_study.study_fixture


def seed(service, prefix="history", elapsed=1):
    return tuple(
        complete(service, f"{prefix}-{i}", elapsed=elapsed)[0]["attempt_id"] for i in range(3)
    )


def request(spec, **options):
    return StudyRequest(workload_ref=spec.ref, strategy="lookup", **options)


def test_empty_snapshot_cannot_acquire_future_experiment_results(study_fixture):
    service, engine, spec = study_fixture
    study = engine.create("team-a", request(spec), "cold")
    assert study["lookup_history"]["profile_refs"] == []
    seed(service)
    assert service.recommend("team-a", spec.ref)["measured"] is True
    engine.tick(study["ref"])
    current = engine.get("team-a", study["ref"])
    assert current["state"] == "ABSTAINED"
    assert current["stop_reason"] == "NO_MEASURED_FEASIBLE_CONFIG"
    assert current["plans"] == []


def test_frozen_cohort_survives_restart_and_excludes_later_results(study_fixture):
    service, engine, spec = study_fixture
    refs = seed(service)
    study = engine.create("team-a", request(spec), "frozen")
    seed(service, "later", elapsed=2)
    assert (
        engine.create("team-a", request(spec), "frozen")["lookup_history"]
        == study["lookup_history"]
    )
    Studies(service).tick(study["ref"])
    current = engine.get("team-a", study["ref"])
    with service.store.transaction() as conn:
        rec = service.store.get(conn, "recommendation", current["lookup_recommendation_ref"])[
            "body"
        ]
    assert set(rec["ranking"][0]["evidence_refs"]) == set(refs)
    assert rec["ranking"][0]["mean_seconds"] == 1
    assert rec["lookup_history"]["cohort_digest"] == study["lookup_history"]["cohort_digest"]
    assert current["confirmation_schedule"] == ["base"] * spec.quality.minimum_repeats
    backend = SchedulerDouble()
    backend.observation = Observation(State.COLLECTING)
    worker = Worker(service, {("team-a", "lab"): backend})
    for _ in range(40):
        engine.tick(study["ref"])
        worker.submit_one()
        worker.reconcile_all()
        current = engine.get("team-a", study["ref"])
        if current["state"] == "COMPLETED":
            break
    assert current["state"] == "COMPLETED"
    assert len(current["observations"]) == spec.quality.minimum_repeats
    assert set(refs).isdisjoint(o["attempt_id"] for o in current["observations"])
    assert all(o["mode"] == "confirmation" for o in current["observations"])


def test_explicit_cohort_stays_identical_across_later_study_creation(study_fixture):
    service, engine, spec = study_fixture
    refs = seed(service)
    req = request(spec, lookup_profile_refs=refs)
    first = engine.create("team-a", req, "first")
    seed(service, "other-study", elapsed=3)
    second = engine.create("team-a", req, "second")
    assert first["lookup_history"]["cohort_digest"] == second["lookup_history"]["cohort_digest"]
    assert (
        set(first["lookup_history"]["profile_refs"])
        == set(second["lookup_history"]["profile_refs"])
        == set(refs)
    )
    empty = engine.create("team-a", request(spec, lookup_profile_refs=()), "empty")
    assert empty["lookup_history"]["profile_refs"] == []


@pytest.mark.parametrize("scope", ["missing", "other-project", "other-workload"])
def test_unknown_or_wrong_scope_profile_is_rejected_without_study(study_fixture, scope):
    service, engine, spec = study_fixture
    ref = seed(service)[0]
    with service.store.transaction() as conn:
        row = service.store.get(conn, "profile", ref)
        if scope == "missing":
            ref = "absent-profile"
        elif scope == "other-project":
            conn.execute(
                update(entities)
                .where(entities.c.kind == "profile", entities.c.ref == ref)
                .values(project="team-b")
            )
        else:
            body = copy.deepcopy(row["body"])
            body["result"]["workload_signature"] = signature("another-workload")
            conn.execute(
                update(entities)
                .where(entities.c.kind == "profile", entities.c.ref == ref)
                .values(body=body)
            )
    with pytest.raises((NotFound, Rejected)):
        engine.create("team-a", request(spec, lookup_profile_refs=(ref,)), "invalid")
    with service.store.transaction() as conn:
        assert not conn.execute(select(studies)).all()


@pytest.mark.parametrize("fault", ["deleted", "changed"])
def test_frozen_evidence_loss_abstains_without_dispatch(study_fixture, fault):
    service, engine, spec = study_fixture
    refs = seed(service)
    study = engine.create("team-a", request(spec), "retained")
    with service.store.transaction() as conn:
        condition = (entities.c.kind == "profile") & (entities.c.ref == refs[0])
        if fault == "deleted":
            conn.execute(delete(entities).where(condition))
        else:
            body = copy.deepcopy(service.store.get(conn, "profile", refs[0])["body"])
            body["result"]["measurements"]["elapsed_seconds"] = 0.001
            conn.execute(update(entities).where(condition).values(body=body))
    engine.tick(study["ref"])
    current = engine.get("team-a", study["ref"])
    assert (
        current["state"] == "ABSTAINED" and current["stop_reason"] == "LOOKUP_HISTORY_UNAVAILABLE"
    )
    assert current["plans"] == []


def test_expired_profiles_are_not_made_valid_by_freezing(study_fixture, monkeypatch):
    import resource_advisor.service as module

    service, engine, spec = study_fixture
    seed(service)
    study = engine.create("team-a", request(spec), "old")
    future = now() + timedelta(seconds=spec.quality.max_profile_age_seconds + 1)
    monkeypatch.setattr(module, "now", lambda: future)
    engine.tick(study["ref"])
    current = engine.get("team-a", study["ref"])
    assert current["state"] == "ABSTAINED" and current["plans"] == []


def test_legacy_unfrozen_lookup_fails_closed_but_keeps_idempotency(study_fixture):
    service, engine, spec = study_fixture
    req = request(spec)
    study = engine.create("team-a", req, "legacy")
    with service.store.transaction() as conn:
        row = service.store.study(conn, study["ref"])
        body = copy.deepcopy(row["body"])
        del body["lookup_history"]
        assert "lookup_profile_refs" not in body["request"]
        service.store.change_study(conn, row, row["state"], body)
    assert engine.create("team-a", req, "legacy")["ref"] == study["ref"]
    engine.tick(study["ref"])
    current = engine.get("team-a", study["ref"])
    assert current["stop_reason"] == "LOOKUP_HISTORY_NOT_FROZEN"
    with service.store.transaction() as conn:
        assert not conn.execute(select(jobs)).all()


def test_lookup_options_cannot_affect_another_strategy_or_duplicate_samples():
    with pytest.raises(ValueError, match="lookup strategy"):
        StudyRequest(workload_ref="w", strategy="qlognei", lookup_profile_refs=("p",))
    with pytest.raises(ValueError, match="duplicate"):
        StudyRequest(workload_ref="w", strategy="lookup", lookup_profile_refs=("p", "p"))
