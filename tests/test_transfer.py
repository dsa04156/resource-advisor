"""Source provenance and durable transfer tests with an explicit scheduler double.

Hand-built hardware-shaped envelopes exercise validation, not physical GPU work.
"""

import hashlib
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation
from resource_advisor.contracts import (
    ProfilingPolicy,
    State,
    StudyRequest,
    now,
    signature,
)
from resource_advisor.search import device_unit
from resource_advisor.service import NotFound, Rejected
from resource_advisor.store import entities
from resource_advisor.study import Studies
from resource_advisor.transfer import TransferEvidenceRequest, TransferSpace, TransferSpaces
from resource_advisor.worker import Worker


class TransferSchedulerDouble(SchedulerDouble):
    source_winner = None

    def result(self, job):
        envelope = super().result(job)
        result = envelope["result"]
        cpu = job["body"]["candidate"]["context"]["resources"]["host_cpu"]
        result["evidence_kind"] = "hardware"  # Validator fixture, never real GPU evidence.
        result["measurements"].update(elapsed_seconds=0.00003 / cpu, peak_memory_mib=100 + cpu)
        if self.source_winner is not None:
            result["measurements"]["elapsed_seconds"] = 1e-5 if cpu == self.source_winner else 3e-5
        return {"result": result, "digest": signature(result)}


@pytest.fixture
def transfer_fixture(service, bundle):
    original, base, variant, _ = bundle
    for ref, shape, dataset in [
        ("target", (1, 3, 32, 32), "target-data"),
        ("source", (1, 3, 16, 16), "source-data"),
    ]:
        identity = original.identity.model_copy(
            update={"input_shape": shape, "dataset_version": dataset}
        )
        v = variant.model_copy(
            update={
                "ref": "variant-" + ref,
                "workload_ref": ref,
                "workload_signature": signature(identity),
                "supported_shapes": (shape,),
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
            for name, cpu in [("base", 1), ("middle", 2), ("other", 3)]
        )
        spec = original.model_copy(
            update={
                "ref": ref,
                "identity": identity,
                "candidates": candidates,
                "profiling": ProfilingPolicy(
                    consent=True,
                    max_candidates=3,
                    max_probes=7,
                    mutable_parameters=("host_cpu",),
                    total_wall_seconds=3600,
                    device_seconds={device_unit(base, v): 3600},
                ),
            }
        )
        service.register("variant", v, "team-a")
        service.register("workload", spec, "team-a")
    request = TransferSpace(
        ref="transfer",
        project_ref="team-a",
        target_workload_ref="target",
        source_workload_refs=("source",),
        feature_names=("host_cpu",),
    )
    backend = TransferSchedulerDouble()
    backend.observation = Observation(State.COLLECTING)
    return (
        service,
        TransferSpaces(service),
        request,
        Worker(service, {("team-a", "lab"): backend}),
        backend,
    )


def source_jobs(fixture, workload="source"):
    _, _, _, _, backend = fixture
    by_candidate = {ref: [] for ref in ("base", "middle", "other")}

    def balanced(_strategy, candidates, observations, _quality, _seed):
        candidate = min(
            candidates, key=lambda c: sum(o["candidate_ref"] == c.ref for o in observations)
        )
        return {
            "candidate_ref": candidate.ref,
            "reason": "TEST_INITIAL_DESIGN",
            "planning_seconds": 0,
        }

    # Two prior consented studies yield independent confirmations for all three
    # configurations; normal non-baseline approval rules stay intact.
    try:
        for winner in (2, 3):
            backend.source_winner = winner
            with patch("resource_advisor.study.ask", side_effect=balanced):
                completed = run_study(
                    fixture,
                    StudyRequest(workload_ref=workload, strategy="random"),
                    key=f"{workload}-prior-{winner}",
                )
            assert completed["state"] == "COMPLETED"
            for obs in completed["observations"]:
                if obs["mode"] == "confirmation":
                    by_candidate[obs["candidate_ref"]].append(obs["job_id"])
    finally:
        backend.source_winner = None
    assert all(len(rows) >= 2 for rows in by_candidate.values())
    return [job_id for rows in by_candidate.values() for job_id in rows[:2]]


def bind(fixture):
    _, spaces, request, _, _ = fixture
    space = spaces.create("team-a", request)
    ids = source_jobs(fixture)
    evidence = spaces.evidence(
        "team-a", request.ref, TransferEvidenceRequest(ref="sources", job_ids=tuple(ids))
    )
    return space, evidence


def run_study(fixture, request, key="transfer-study"):
    service, _, _, worker, _ = fixture
    study = Studies(service).create("team-a", request, key)
    assert Studies(service).create("team-a", request, key)["ref"] == study["ref"]
    for _ in range(100):
        Studies(service).tick(study["ref"])
        worker.submit_one()
        worker.reconcile_all()
        current = Studies(service).get("team-a", study["ref"])
        if current["state"] in {"COMPLETED", "ABSTAINED", "FAILED"}:
            return current
    pytest.fail("transfer study did not terminate")


def command(strategy="rgpe"):
    return StudyRequest(
        workload_ref="target",
        strategy=strategy,
        transfer_space_ref="transfer",
        transfer_evidence_ref="sources",
        seed=7,
    )


def test_immutable_registration_provenance_and_no_source_profile_copy(transfer_fixture):
    service, spaces, request, _, backend = transfer_fixture
    space, evidence = bind(transfer_fixture)
    assert spaces.create("team-a", request) == space
    assert (
        spaces.evidence(
            "team-a", request.ref, TransferEvidenceRequest.model_validate(evidence["request"])
        )
        == evidence
    )
    assert len(evidence["provenance"]) == 6
    assert backend.submissions == 26
    assert evidence["execution_authorized"] is False
    with service.store.transaction() as conn:
        assert spaces.checked(conn, "team-a", "transfer", "sources") == (space, evidence)
        profiles = service.store.list(conn, "profile", "team-a")
        assert len(profiles) == 12
        assert len({p["body"]["result"]["workload_signature"] for p in profiles}) == 1
    with pytest.raises(Rejected, match="immutable"):
        spaces.create("team-a", request.model_copy(update={"maximum_input_elements_ratio": 8}))


@pytest.mark.parametrize("strategy", ["rgpe", "history_warm_start"])
def test_real_kernel_durable_loop_holds_out_target_confirmations(transfer_fixture, strategy):
    pytest.importorskip("botorch")
    service, _, _, _, _ = transfer_fixture
    _, evidence = bind(transfer_fixture)
    current = run_study(transfer_fixture, command(strategy))
    assert current["state"] == "COMPLETED", current
    probes = [o for o in current["observations"] if o["mode"] == "pilot"]
    confirms = [o for o in current["observations"] if o["mode"] == "confirmation"]
    assert len(probes) == 7 and len(confirms) == 6
    source_ids = {p["attempt_id"] for p in evidence["provenance"]}
    probe_ids, confirm_ids = {o["attempt_id"] for o in probes}, {o["attempt_id"] for o in confirms}
    assert source_ids.isdisjoint(probe_ids | confirm_ids)
    assert probe_ids.isdisjoint(confirm_ids)
    with service.store.transaction() as conn:
        plans = [service.store.get(conn, "probe_plan", ref)["body"] for ref in current["plans"]]
        profiles = service.store.list(conn, "profile", "team-a")
    assert {p["workload_ref"] for p in plans} == {"target"}
    assert len(profiles) == 18  # Prior source confirmations and fresh target confirmations only.
    choices = [p["choice"] for p in plans if p["mode"] == "pilot"]
    if strategy == "rgpe":
        assert [c["reason"] for c in choices[:6]] == ["TARGET_CHECKS_REQUIRED"] * 6
        assert choices[-1]["method"] == "rank_weighted_gp_ensemble_qLogNEI"
        assert set(choices[-1]["surrogate"]["training_run_ids"]) < probe_ids
        assert set(choices[-1]["surrogate"]["source_run_ids"]) == source_ids
        assert confirm_ids.isdisjoint(choices[-1]["surrogate"]["training_run_ids"])
    else:
        assert choices[0]["candidate_ref"] == "middle"  # Source tie uses stable ref ordering.
        assert choices[0]["reason"] == "HISTORY_GUIDED_INITIAL_CANDIDATE"
        assert choices[3]["transfer_phase"] == "TARGET_ONLY_BO_AFTER_WARM_START"
        assert all("weights" not in c for c in choices)
    assert current["historical_source_cost_recharged"] is False
    assert sum(current["charged_device_seconds"].values()) == pytest.approx(
        sum(o["device_seconds"] for o in current["observations"])
    )
    assert current["planning_seconds"] > 0


@pytest.mark.parametrize(
    "case", ["synthetic", "epoch", "wrong_profile", "stale", "pilot", "quality"]
)
def test_source_evidence_requires_fresh_independent_hardware_profile(transfer_fixture, case):
    service, spaces, request, _, _ = transfer_fixture
    spaces.create("team-a", request)
    ids = source_jobs(transfer_fixture)
    with service.store.transaction() as conn:
        row = service.store.job(conn, ids[0])
        body = row["body"]
        result = service.store.get(conn, "result", body["attempt_id"])["body"]
        if case == "stale":
            body["finished_at"] = (now() - timedelta(days=2)).isoformat()
        elif case == "pilot":
            body["request"]["mode"] = "pilot"
        elif case == "wrong_profile":
            profile = service.store.get(conn, "profile", body["attempt_id"])["body"]
            profile["job_id"] = "wrong-job"
            conn.execute(
                update(entities)
                .where(entities.c.kind == "profile", entities.c.ref == body["attempt_id"])
                .values(body=profile)
            )
        else:
            if case == "synthetic":
                result["evidence_kind"] = "synthetic"
            elif case == "epoch":
                result["epoch"] += 1
            else:
                result["measurements"]["quality_value"] = 0.1
            body["result_digest"] = signature(result)
            conn.execute(
                update(entities)
                .where(entities.c.kind == "result", entities.c.ref == body["attempt_id"])
                .values(body=result)
            )
        service.store.change_job(conn, row, row["state"], body)
    with pytest.raises(Rejected):
        spaces.evidence(
            "team-a", request.ref, TransferEvidenceRequest(ref="bad", job_ids=tuple(ids))
        )


def test_target_jobs_cannot_be_reclassified_as_source_evidence(transfer_fixture):
    _, spaces, request, _, _ = transfer_fixture
    spaces.create("team-a", request)
    ids = source_jobs(transfer_fixture, "target")
    with pytest.raises(Rejected, match="independent measured profile"):
        spaces.evidence(
            "team-a", request.ref, TransferEvidenceRequest(ref="leak", job_ids=tuple(ids))
        )


@pytest.mark.parametrize("case", ["alias", "new_code", "shape", "budget"])
def test_unqualified_family_and_budget_rejected(transfer_fixture, case):
    service, spaces, request, _, _ = transfer_fixture
    if case == "budget":
        ref = "target"
    else:
        ref = "source"
    with service.store.transaction() as conn:
        row = service.store.get(conn, "workload", ref)
        raw = row["body"]
        if case == "alias":
            raw["identity"] = service.store.get(conn, "workload", "target")["body"]["identity"]
        elif case == "new_code":
            raw["identity"]["code_digest"] = signature("different-code")
        elif case == "shape":
            raw["identity"]["input_shape"] = [1, 3, 1024, 1024]
        else:
            raw["profiling"]["max_probes"] = 6
        conn.execute(
            update(entities)
            .where(entities.c.kind == "workload", entities.c.ref == ref)
            .values(body=raw)
        )
    if case == "budget":
        bind(transfer_fixture)
    with pytest.raises(Rejected, match="target checks" if case == "budget" else None):
        if case == "budget":
            Studies(service).create("team-a", command(), "bad-budget")
        else:
            spaces.create("team-a", request)


def test_source_cohort_must_cover_every_configuration(transfer_fixture):
    service, spaces, request, _, _ = transfer_fixture
    spaces.create("team-a", request)
    source_jobs(transfer_fixture)
    with service.store.transaction() as conn:
        ids = tuple(
            p["body"]["job_id"]
            for p in service.store.list(conn, "profile", "team-a")
            if p["body"]["candidate_ref"] == "base"
        )
    assert len(ids) == 6
    with pytest.raises(Rejected, match="every configuration"):
        spaces.evidence(
            "team-a", request.ref, TransferEvidenceRequest(ref="incomplete", job_ids=ids)
        )


def test_source_capability_can_expire_when_target_has_fresh_same_runtime(
    transfer_fixture, bundle, monkeypatch
):
    service, spaces, request, _, _ = transfer_fixture
    ids = source_jobs(transfer_fixture)
    future = now() + timedelta(minutes=10)
    cap = bundle[3].model_copy(update={"ref": "fresh-target", "observed_at": future})
    service.register("capability", cap, "team-a")
    with service.store.transaction() as conn:
        target = service.store.get(conn, "workload", "target")["body"]
        for candidate in target["candidates"]:
            candidate["capability_ref"] = cap.ref
        conn.execute(
            update(entities)
            .where(entities.c.kind == "workload", entities.c.ref == "target")
            .values(body=target)
        )
    monkeypatch.setattr("resource_advisor.policy.now", lambda: future)
    spaces.create("team-a", request)
    evidence = spaces.evidence(
        "team-a", request.ref, TransferEvidenceRequest(ref="historical", job_ids=tuple(ids))
    )
    assert len(evidence["provenance"]) == 6


def test_source_expiry_after_creation_falls_back_to_target_only(transfer_fixture, monkeypatch):
    service, _, _, worker, _ = transfer_fixture
    bind(transfer_fixture)
    study = Studies(service).create("team-a", command(), "expire")
    monkeypatch.setattr("resource_advisor.transfer.now", lambda: now() + timedelta(hours=2))
    for _ in range(2):
        Studies(service).tick(study["ref"])
        worker.submit_one()
        worker.reconcile_all()
    current = Studies(service).get("team-a", study["ref"])
    with service.store.transaction() as conn:
        plan = service.store.get(conn, "probe_plan", current["plans"][0])["body"]
    assert plan["choice"]["transfer_phase"] == "TARGET_ONLY_BO_FALLBACK"
    assert "stale" in plan["choice"]["transfer_fallback"]


def test_model_failure_uses_real_target_fallback_without_fake_rgpe(transfer_fixture, monkeypatch):
    pytest.importorskip("botorch")
    service, _, _, _, _ = transfer_fixture
    bind(transfer_fixture)

    def fail(_):
        raise RuntimeError("controlled RGPE failure")

    monkeypatch.setattr("resource_advisor.rgpe.ask_rgpe", fail)
    current = run_study(transfer_fixture, command())
    assert current["state"] == "COMPLETED"
    with service.store.transaction() as conn:
        plans = [service.store.get(conn, "probe_plan", ref)["body"] for ref in current["plans"]]
    assert all(
        p["choice"]["transfer_fallback"] == "TRANSFER_MODEL_FAILED:RuntimeError"
        for p in plans
        if p["mode"] == "pilot"
    )


def test_operator_routes_scope_and_no_posted_measurements(transfer_fixture):
    service, _, request, _, _ = transfer_fixture
    credentials = {
        hashlib.sha256(k.encode()).hexdigest(): v
        for k, v in {
            "op": Principal("team-a", True),
            "user": Principal("team-a"),
            "other": Principal("team-b"),
        }.items()
    }
    client = TestClient(create_app(service, credentials))
    path = "/api/v1/compute/transfer-spaces"

    def headers(key):
        return {"Authorization": "Bearer " + key}

    assert (
        client.post(path, json=request.model_dump(mode="json"), headers=headers("user")).status_code
        == 403
    )
    assert (
        client.post(path, json=request.model_dump(mode="json"), headers=headers("op")).status_code
        == 200
    )
    assert client.get(path + "/transfer", headers=headers("other")).status_code == 404
    assert (
        client.post(
            path + "/transfer/evidence",
            headers=headers("user"),
            json={"ref": "fake", "job_ids": [f"job-{i}" for i in range(6)], "measurements": []},
        ).status_code
        == 422
    )
    with pytest.raises(NotFound):
        TransferSpaces(service).get("team-b", "transfer")
