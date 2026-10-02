import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from test_worker import SchedulerDouble

from resource_advisor.api import Principal, create_app
from resource_advisor.backends import BackendError, KubernetesBackend, Observation
from resource_advisor.contracts import JobRequest, State, WorkloadSpec, signature
from resource_advisor.service import NotFound
from resource_advisor.store import Conflict
from resource_advisor.training import TrainingIsolation, prepare, validate_receipt
from resource_advisor.worker import Worker


def setup_training(service, bundle, *, binding_override=None):
    spec, candidate, variant, cap = bundle
    checkpoint = {"weights": [[0.0], [0.0]], "bias": [0.0]}
    inputs = {"x": [[1.0, 2.0]], "y": [[3.0]]}

    def encode(x):
        return json.dumps(x, sort_keys=True, separators=(",", ":")).encode()

    cp, inp = encode(checkpoint), encode(inputs)
    cp_digest, inp_digest = signature(checkpoint), signature(inputs)
    identity = spec.identity.model_copy(
        update={"task_type": "training", "model_digest": cp_digest, "dataset_version": inp_digest}
    )
    variant = variant.model_copy(
        update={
            "ref": "train-variant",
            "workload_ref": "train-workload",
            "model_digest": cp_digest,
            "workload_signature": signature(identity),
            "verification": "TRAINING_VERIFIED",
            "pilot_command": variant.command,
        }
    )
    candidate = candidate.model_copy(update={"variant_ref": variant.ref})
    spec = WorkloadSpec.model_validate(
        spec.model_copy(
            update={
                "ref": "train-workload",
                "identity": identity,
                "candidates": (candidate,),
                "profiling": spec.profiling.model_copy(update={"checkpoint_digest": cp_digest}),
            }
        ).model_dump()
    )
    binding = TrainingIsolation(
        ref=spec.ref,
        project_ref="team-a",
        workload_ref=spec.ref,
        workload_digest=signature(spec),
        checkpoint_digest=cp_digest,
        input_digest=inp_digest,
        checkpoint_size_bytes=len(cp),
        input_size_bytes=len(inp),
        validation_refs=("isolated-fixture",),
    )
    if binding_override:
        binding = binding.model_copy(update=binding_override)
    for kind, model in [("workload", spec), ("variant", variant), ("training_isolation", binding)]:
        service.register(kind, model, "team-a")
    return spec, binding, cp, inp


def test_source_copies_are_distinct_and_originals_unchanged(service, bundle, tmp_path):
    _, binding, cp, inp = setup_training(service, bundle)
    source = tmp_path / "source"
    source.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (source / "checkpoint.json").write_bytes(cp)
    (source / "input.json").write_bytes(inp)
    prepare(binding, source, work)
    assert (work / "checkpoint/checkpoint.json").read_bytes() == cp
    assert (work / "checkpoint/checkpoint.json").stat().st_ino != (
        source / "checkpoint.json"
    ).stat().st_ino
    assert (work / "checkpoint/checkpoint.json").stat().st_mode & 0o777 == 0o400
    (work / "output/final.json").write_text('{"step":10}')
    assert (source / "checkpoint.json").read_bytes() == cp
    assert (source / "input.json").read_bytes() == inp
    with pytest.raises(FileExistsError):
        prepare(binding, source, work)


@pytest.mark.parametrize("failure", ["digest", "symlink"])
def test_preparation_rejects_mutated_or_symlinked_sources(service, bundle, tmp_path, failure):
    _, binding, cp, inp = setup_training(service, bundle)
    source = tmp_path / "source"
    source.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (source / "input.json").write_bytes(inp)
    if failure == "digest":
        (source / "checkpoint.json").write_bytes(cp + b" ")
    else:
        (tmp_path / "target").write_bytes(cp)
        (source / "checkpoint.json").symlink_to(tmp_path / "target")
    with pytest.raises((ValueError, OSError)):
        prepare(binding, source, work)
    assert list(work.iterdir()) == []


def test_operator_binding_and_backend_source_required(service, bundle):
    spec, binding, _, _ = setup_training(service, bundle)
    job = service.submit("team-a", JobRequest(workload_ref=spec.ref, candidate_ref="base"), "train")
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
    backend = KubernetesBackend(
        namespace="research-a", local_queue="batch", node_selector={"pool": "lab"}
    )
    with pytest.raises(BackendError, match="source binding"):
        backend.manifest(row)
    backend.training_sources = {
        binding.ref: {
            "binding_digest": signature(binding),
            "source": {"config_map": "approved-source"},
            "checkpoint_key": "checkpoint.json",
            "input_key": "input.json",
        }
    }
    manifest = backend.manifest(row)
    pod = manifest["spec"]["template"]["spec"]
    main = pod["containers"][0]
    init = pod["initContainers"][0]
    assert not any(v["name"] == "training-original" for v in main["volumeMounts"])
    assert all(v["readOnly"] for v in init["volumeMounts"] if v["name"] == "training-original")
    assert all(
        v.get("readOnly")
        for v in main["volumeMounts"]
        if v.get("subPath") in {"checkpoint", "input"}
    )
    assert (
        main["securityContext"]["readOnlyRootFilesystem"] and pod["securityContext"]["runAsNonRoot"]
    )
    assert not pod["automountServiceAccountToken"]
    assert manifest["spec"]["suspend"] and main["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert not any("gpu" in key for key in init["resources"]["requests"])
    backend.training_sources[binding.ref]["checkpoint_key"] = "../original"
    with pytest.raises(BackendError):
        backend.manifest(row)


def test_unbound_training_never_submits(service, bundle):
    spec, _, variant, _ = bundle
    identity = spec.identity.model_copy(update={"task_type": "training"})
    variant = variant.model_copy(
        update={
            "ref": "unbound-v",
            "workload_ref": "unbound",
            "verification": "TRAINING_VERIFIED",
            "workload_signature": signature(identity),
        }
    )
    spec = spec.model_copy(
        update={
            "ref": "unbound",
            "identity": identity,
            "candidates": (spec.candidates[0].model_copy(update={"variant_ref": variant.ref}),),
        }
    )
    service.register("workload", spec, "team-a")
    service.register("variant", variant, "team-a")
    with pytest.raises(NotFound):
        service.submit("team-a", JobRequest(workload_ref=spec.ref, candidate_ref="base"), "unbound")


def completed_training(service, bundle):
    spec, binding, _, _ = setup_training(service, bundle)
    job = service.submit("team-a", JobRequest(workload_ref=spec.ref, candidate_ref="base"), "train")
    backend = SchedulerDouble()
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
    envelope = backend.result(row)
    result = envelope["result"]
    receipt = {
        "job_id": job["job_id"],
        "attempt_id": job["attempt_id"],
        "result_digest": envelope["digest"],
        "initial_checkpoint_digest": binding.checkpoint_digest,
        "input_digest": binding.input_digest,
        "checkpoint_after_digest": binding.checkpoint_digest,
        "input_after_digest": binding.input_digest,
        "checkpoint_readonly": True,
        "input_readonly": True,
        "source_not_mounted": True,
        "output_checkpoint": {"step": 10},
        "output_checkpoint_digest": signature({"step": 10}),
    }
    backend.result = lambda _: dict(envelope, training_receipt=receipt)
    worker = Worker(service, {("team-a", "lab"): backend})
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    return job, result, receipt, binding


def test_training_receipt_is_atomic_immutable_and_project_scoped(service, bundle):
    from resource_advisor.contracts import ExecutionResult

    job, result, receipt, _ = completed_training(service, bundle)
    assert service.get_job("team-a", job["job_id"])["state"] == "SUCCEEDED"
    with service.store.transaction() as conn:
        assert service.store.get(conn, "training_receipt", job["attempt_id"])["body"] == receipt
    service.ingest(
        "team-a",
        ExecutionResult.model_validate(result),
        signature(result),
        training_receipt=receipt,
    )
    with pytest.raises(Conflict):
        service.ingest(
            "team-a",
            ExecutionResult.model_validate(result),
            signature(result),
            training_receipt=dict(receipt, input_readonly=False),
        )
    creds = {hashlib.sha256(k.encode()).hexdigest(): Principal(k) for k in ("team-a", "team-b")}
    client = TestClient(create_app(service, creds))
    path = "/api/v1/compute/jobs/" + job["job_id"] + "/training-receipt"
    assert client.get(path, headers={"Authorization": "Bearer team-a"}).json() == receipt
    assert client.get(path, headers={"Authorization": "Bearer team-b"}).status_code == 404
    assert (
        client.post(
            "/api/v1/compute/training-isolation",
            json={},
            headers={"Authorization": "Bearer team-a"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "change",
    [
        {"checkpoint_after_digest": signature("changed")},
        {"input_readonly": False},
        {"output_checkpoint": {"step": 99}},
        {"job_id": "another-job"},
    ],
)
def test_receipt_mutation_and_false_protection_rejected(service, bundle, change):
    from resource_advisor.contracts import ExecutionResult

    _, result, receipt, binding = completed_training(service, bundle)
    with pytest.raises(ValueError):
        validate_receipt(dict(receipt, **change), ExecutionResult.model_validate(result), binding)


def test_missing_training_receipt_cannot_complete(service, bundle):
    spec, _, _, _ = setup_training(service, bundle)
    job = service.submit(
        "team-a", JobRequest(workload_ref=spec.ref, candidate_ref="base"), "no-receipt"
    )
    backend = SchedulerDouble()
    worker = Worker(service, {("team-a", "lab"): backend})
    worker.submit_one()
    backend.observation = Observation(State.COLLECTING)
    worker.reconcile_all()
    assert service.get_job("team-a", job["job_id"])["state"] == "COLLECTING"
    with service.store.transaction() as conn:
        assert service.store.get(conn, "result", job["attempt_id"]) is None
        assert service.store.get(conn, "training_receipt", job["attempt_id"]) is None


def test_binding_rejects_semantic_mutation_and_other_checkpoint(service, bundle):
    from resource_advisor.training import validate_binding

    spec, binding, _, _ = setup_training(service, bundle)
    with service.store.transaction() as conn:
        _, candidate, variant, _ = service.bundle(conn, "team-a", spec.ref, "base")
    changed = spec.model_copy(
        update={
            "profiling": spec.profiling.model_copy(update={"mutable_parameters": ("optimizer",)})
        }
    )
    updated = binding.model_copy(update={"workload_digest": signature(changed)})
    with pytest.raises(ValueError, match="semantic"):
        validate_binding(updated, changed, candidate, variant)
    with pytest.raises(ValueError, match="does not match"):
        validate_binding(
            binding.model_copy(update={"checkpoint_digest": signature("other")}),
            spec,
            candidate,
            variant,
        )


def test_slurm_training_is_explicitly_unqualified(service, bundle):
    from resource_advisor.backends import SlurmBackend

    spec, _, _, _ = setup_training(service, bundle)
    job = service.submit(
        "team-a", JobRequest(workload_ref=spec.ref, candidate_ref="base"), "slurm-guard"
    )
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
    backend = SlurmBackend(partition="gpu", account="team-a", qos="normal", output_dir="/tmp/ra")
    with pytest.raises(BackendError, match="checkpoint isolation"):
        backend.script(row)


def test_bad_training_binding_abstains_without_breaking_console(service, bundle):
    from resource_advisor.console import overview

    spec, _, _, _ = setup_training(
        service, bundle, binding_override={"checkpoint_digest": signature("other")}
    )
    rec = service.recommend("team-a", spec.ref)
    assert rec["status"] == "NO_COMPATIBLE_VARIANT"
    row = next(
        x
        for x in overview(service, "team-a")["compatibility"]["items"]
        if x["workload_ref"] == spec.ref
    )
    assert row["candidates"][0]["reasons"] == ["QUALIFICATION_REJECTED"]


@pytest.mark.parametrize(
    "sources", [["not-a-map"], {"workload": "not-a-route"}, {"workload": {"source": None}}]
)
def test_malformed_route_rejected_locally(sources):
    with pytest.raises(ValueError, match="training sources"):
        KubernetesBackend(
            namespace="lab",
            local_queue="queue",
            node_selector={"pool": "lab"},
            training_sources=sources,
        )
