"""Real byte consumption with explicit scheduler/storage doubles, not GPU evidence."""

import hashlib
import json
import os
from collections import Counter

import pytest
from fastapi.testclient import TestClient
from test_tracking import TrackingServer, delivery
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import Observation, identity_environment, result_from_log
from resource_advisor.contracts import (
    ExecutionResult,
    JobRequest,
    State,
    WorkloadIdentity,
    signature,
)
from resource_advisor.sampling import (
    SamplingBindingRequest,
    SamplingPolicies,
    SamplingPolicy,
    SamplingSession,
    TensorBatch,
    bind,
    directory_reader,
    selection,
    validate_receipt,
)
from resource_advisor.service import Rejected
from resource_advisor.store import Conflict
from resource_advisor.worker import Worker


@pytest.fixture
def sampled(service, bundle, tmp_path):
    spec, candidate, variant, _ = bundle
    population = []
    for i in range(10):
        raw = (
            TensorBatch(shape=(1, 2), precision="fp32", values=(float(i), float(i + 1)))
            .model_dump_json()
            .encode()
        )
        ref = f"input-{i}"
        (tmp_path / (ref + ".json")).write_bytes(raw)
        population.append(
            {
                "ref": ref,
                "stratum": "large" if i < 6 else "small",
                "content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }
        )
    policy = SamplingPolicy(
        ref="sampling-v1",
        project_ref="team-a",
        dataset_version="sampled-v1",
        input_shape=(1, 2),
        precision="fp32",
        batch_size=1,
        population=tuple(population),
    )
    identity = spec.identity.model_copy(
        update={
            "input_shape": (1, 2),
            "dataset_version": policy.dataset_version,
            "sampling_policy_digest": signature(policy),
        }
    )
    variant = variant.model_copy(
        update={
            "ref": "sampled-runtime",
            "workload_ref": "sampled",
            "workload_signature": signature(identity),
            "supported_shapes": ((1, 2),),
        }
    )
    spec = spec.model_copy(
        update={
            "ref": "sampled",
            "identity": identity,
            "candidates": (candidate.model_copy(update={"variant_ref": variant.ref}),),
        }
    )
    for kind, value in [("sampling_policy", policy), ("variant", variant), ("workload", spec)]:
        service.register(kind, value, "team-a")
    return policy, spec, tmp_path


def register_binding(service, sampled):
    policy, spec, _ = sampled
    return SamplingPolicies(service).bind(
        "team-a", SamplingBindingRequest(workload_ref=spec.ref, policy_ref=policy.ref)
    )


def start(service, sampled):
    register_binding(service, sampled)
    job = service.submit(
        "team-a", JobRequest(workload_ref="sampled", candidate_ref="base"), "sample"
    )
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    worker.submit_one()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
    return backend, worker, row


def consume(sampled, row, backend):
    session = SamplingSession(row["body"]["sampling_binding"]["plan"])
    reader = directory_reader(sampled[2])
    seen = []

    def operation(batch):
        seen.append(batch.values)
        return sum(batch.values)

    session.warmup(reader, operation)
    for _ in range(session.plan.work_units):
        session.measure_next(reader, operation)
    assert len(seen) == session.plan.work_units + session.plan.warmup_units
    assert len(session.measured) == session.plan.work_units
    envelope = backend.result(row)
    result = ExecutionResult.model_validate(envelope["result"])
    envelope["sampling_receipt"] = session.receipt(result).model_dump(mode="json")
    return envelope


def test_selection_preserves_declared_proportions_and_deterministic_order(sampled):
    policy, spec, _ = sampled
    short = selection(policy, 5)
    assert Counter(s.stratum for s in short) == {"large": 3, "small": 2}
    assert set(s.ref for s in short) <= set(s.ref for s in selection(policy, 10))
    assert short == selection(policy.model_copy(update={"population": policy.population[::-1]}), 5)
    assert short[0] == selection(policy, 10)[0]
    assert bind(policy, spec).plan.population_size == 10
    with pytest.raises(ValueError, match="tolerance"):
        selection(policy, 2)
    with pytest.raises(ValueError, match="every declared stratum"):
        selection(policy, 1)
    for invalid in [True, 11, 0, 2.5]:
        with pytest.raises(ValueError, match="budget"):
            selection(policy, invalid)
    duplicate = policy.model_dump(mode="json")
    duplicate["population"][1]["content_digest"] = duplicate["population"][0]["content_digest"]
    with pytest.raises(ValueError, match="repetition"):
        SamplingPolicy.model_validate(duplicate)


def test_legacy_identity_is_byte_compatible_and_sampled_execution_requires_binding(
    service, sampled, bundle
):
    identity = bundle[0].identity
    old = identity.model_dump(mode="json")
    assert "sampling_policy_digest" not in old
    assert signature(identity) == signature(old)
    assert (
        WorkloadIdentity.model_validate({**old, "sampling_policy_digest": None}).model_dump(
            mode="json"
        )
        == old
    )
    assert signature(sampled[1].identity) != signature(identity)
    with pytest.raises(Rejected, match="sampling binding required"):
        service.submit(
            "team-a", JobRequest(workload_ref="sampled", candidate_ref="base"), "unbound"
        )
    legacy = sampled[1].model_copy(
        update={"identity": sampled[1].identity.model_copy(update={"sampling_policy_digest": None})}
    )
    with pytest.raises(ValueError, match="identity"):
        bind(sampled[0], legacy)
    binding = register_binding(service, sampled)
    assert register_binding(service, sampled) == binding


def test_consumed_bytes_reach_worker_database_tracking_and_artifact(service, sampled):
    from test_artifacts import MemoryS3

    from resource_advisor.artifacts import ArtifactDelivery, S3Artifacts

    backend, worker, row = start(service, sampled)
    envelope = consume(sampled, row, backend)
    assert (
        json.loads(identity_environment(row)["RA_SAMPLING_PLAN_JSON"])
        == row["body"]["sampling_binding"]["plan"]
    )
    backend.result = lambda _: envelope
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    assert service.get_job("team-a", row["id"])["state"] == State.SUCCEEDED
    with service.store.transaction() as conn:
        saved = service.store.get(conn, "sampling_receipt", row["body"]["attempt_id"])["body"]
    assert saved == envelope["sampling_receipt"]
    server = TrackingServer()
    assert delivery(service, server).deliver_one()
    tags = {t["key"]: t["value"] for t in server.batches[0]["tags"]}
    assert tags["sampling.receipt_digest"] == signature(saved)
    assert tags["sampling.policy_digest"] == signature(sampled[0])
    storage = S3Artifacts(buckets={"team-a": "test-results"}, client=MemoryS3())
    assert ArtifactDelivery(service.store, storage).deliver_one()
    with service.store.transaction() as conn:
        record = service.store.list(conn, "artifact", "team-a")[0]["body"]
    artifact = json.loads(storage.read(record))
    assert artifact["sampling_receipt"] == saved
    assert artifact["sampling_policy"] == sampled[0].model_dump(mode="json")
    assert artifact["sampling_binding"] == row["body"]["sampling_binding"]
    result = ExecutionResult.model_validate(envelope["result"])
    altered = {**saved, "warmup_units": saved["warmup_units"] + 1}
    with pytest.raises(Conflict, match="immutable"):
        service.ingest("team-a", result, signature(result), sampling_receipt=altered)


@pytest.mark.parametrize(
    "alteration", ["missing", "reorder", "duplicate", "attempt", "result", "warmup"]
)
def test_invalid_receipts_do_not_create_results_or_profiles(service, sampled, alteration):
    backend, worker, row = start(service, sampled)
    envelope = consume(sampled, row, backend)
    receipt = envelope["sampling_receipt"]
    if alteration == "missing":
        envelope.pop("sampling_receipt")
    elif alteration == "reorder":
        receipt["measured_samples"].reverse()
    elif alteration == "duplicate":
        receipt["measured_samples"][1] = receipt["measured_samples"][0]
    elif alteration == "attempt":
        receipt["attempt_id"] = "another-attempt"
    elif alteration == "result":
        receipt["result_digest"] = signature("another-result")
    else:
        receipt["warmup_units"] += 1
    backend.result = lambda _: envelope
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    assert service.get_job("team-a", row["id"])["state"] == State.COLLECTING
    with service.store.transaction() as conn:
        assert service.store.get(conn, "result", row["body"]["attempt_id"]) is None
        assert service.store.list(conn, "sampling_receipt", "team-a") == []
        assert service.store.list(conn, "profile", "team-a") == []


@pytest.mark.parametrize(
    "fault", ["changed", "oversize", "symlink", "fifo", "shape", "precision", "operation"]
)
def test_input_or_operation_failure_cannot_emit_receipt_or_resume(sampled, fault):
    policy, spec, root = sampled
    binding = bind(policy, spec)
    first = binding.plan.samples[0]
    path = root / (first.ref + ".json")
    if fault == "changed":
        path.write_bytes(b"{}")
    elif fault == "oversize":
        path.write_bytes(b"x" * 1048577)
    elif fault == "symlink":
        path.unlink()
        path.symlink_to(root / (binding.plan.samples[1].ref + ".json"))
    elif fault == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif fault in {"shape", "precision"}:
        raw = (
            TensorBatch(
                shape=(2,) if fault == "shape" else (1, 2),
                precision="fp16" if fault == "precision" else "fp32",
                values=(1.0, 2.0),
            )
            .model_dump_json()
            .encode()
        )
        path.write_bytes(raw)
        # Approve the bytes but deliberately leave policy shape/precision unchanged.
        pop = tuple(
            s.model_copy(update={"content_digest": "sha256:" + hashlib.sha256(raw).hexdigest()})
            if s.ref == first.ref
            else s
            for s in policy.population
        )
        policy = policy.model_copy(update={"population": pop})
        spec = spec.model_copy(
            update={
                "identity": spec.identity.model_copy(
                    update={"sampling_policy_digest": signature(policy)}
                )
            }
        )
        binding = bind(policy, spec)
    session = SamplingSession(binding.plan)

    def operation(batch):
        if fault == "operation":
            raise RuntimeError("injected operation failure")
        return sum(batch.values)

    reader = directory_reader(root)
    with pytest.raises((ValueError, OSError, RuntimeError)):
        session.warmup(reader, operation)
        for _ in range(session.plan.work_units):
            session.measure_next(reader, operation)
    assert session.failed
    with pytest.raises(ValueError):
        session.measure_next(reader, operation)
    with pytest.raises(ValueError, match="entirely consumed"):
        session.receipt(None)


def test_session_enforces_warmup_and_complete_budget(service, sampled):
    backend, _, row = start(service, sampled)
    session = SamplingSession(row["body"]["sampling_binding"]["plan"])
    reader = directory_reader(sampled[2])

    def operation(batch):
        return sum(batch.values)

    with pytest.raises(ValueError, match="warmup"):
        session.measure_next(reader, operation)
    session.warmup(reader, operation)
    with pytest.raises(ValueError, match="once"):
        session.warmup(reader, operation)
    result = ExecutionResult.model_validate(backend.result(row)["result"])
    with pytest.raises(ValueError, match="entirely consumed"):
        session.receipt(result)
    for _ in range(10):
        session.measure_next(reader, operation)
    with pytest.raises(ValueError, match="exhausted"):
        session.measure_next(reader, operation)
    assert validate_receipt(session.receipt(result), result, row["body"]["sampling_binding"])


def test_policy_operator_approval_and_project_scoped_readback(service, sampled):
    tokens = {
        hashlib.sha256(k.encode()).hexdigest(): p
        for k, p in {
            "owner": Principal("team-a", True),
            "user": Principal("team-a"),
            "other": Principal("team-b", True),
        }.items()
    }
    policy, spec, _ = sampled
    with TestClient(create_app(service, tokens)) as client:
        path = "/api/v1/compute/sampling-policies"

        def headers(who):
            return {"Authorization": "Bearer " + who}

        assert (
            client.post(
                path, json=policy.model_dump(mode="json"), headers=headers("user")
            ).status_code
            == 403
        )
        assert (
            client.post(
                path, json=policy.model_dump(mode="json"), headers=headers("owner")
            ).status_code
            == 200
        )
        assert client.get(
            path + "/" + policy.ref, headers=headers("user")
        ).json() == policy.model_dump(mode="json")
        assert client.get(path + "/" + policy.ref, headers=headers("other")).status_code == 404
        request = {"workload_ref": spec.ref, "policy_ref": policy.ref}
        path = "/api/v1/compute/sampling-bindings"
        assert client.post(path, json=request, headers=headers("other")).status_code == 404
        assert client.post(path, json=request, headers=headers("user")).status_code == 403
        assert client.post(path, json=request, headers=headers("owner")).status_code == 200
        backend, worker, row = start(service, sampled)
        path = f"/api/v1/compute/jobs/{row['id']}/sampling-receipt"
        assert client.get(path, headers=headers("user")).status_code == 404
        envelope = consume(sampled, row, backend)
        backend.result = lambda _: envelope
        backend.observation = Observation(State.COLLECTING)
        worker.reconcile_all()
        assert client.get(path, headers=headers("user")).json() == envelope["sampling_receipt"]
        assert client.get(path, headers=headers("other")).status_code == 404


def test_bounded_tensor_and_maximum_receipt_fit_transport(sampled):
    for precision, value in [("fp16", 65505.0), ("fp32", 3.5e38), ("fp32", float("nan"))]:
        with pytest.raises(ValueError):
            TensorBatch(shape=(1,), precision=precision, values=(value,))
    policy, spec, _ = sampled
    population = tuple(
        {"ref": f"s{i:03}" + "x" * 92, "stratum": "g" + str(i % 2), "content_digest": signature(i)}
        for i in range(128)
    )
    policy = SamplingPolicy.model_validate({**policy.model_dump(), "population": population})
    spec = spec.model_copy(
        update={
            "identity": spec.identity.model_copy(
                update={"work_units": 128, "sampling_policy_digest": signature(policy)}
            )
        }
    )
    plan = bind(policy, spec).plan
    assert len(plan.model_dump_json().encode()) < 131072
    receipt = {
        "measured_samples": [
            {"ref": s.ref, "content_digest": s.content_digest} for s in plan.samples
        ]
    }
    envelope = {"result": {}, "digest": signature("fixture"), "sampling_receipt": receipt}
    raw = json.dumps(envelope)
    assert len(raw) < 60000  # Leave room for the bounded ExecutionResult.
    assert result_from_log("RESOURCE_ADVISOR_RESULT " + raw) == envelope
