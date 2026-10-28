import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from resource_advisor import live_training
from resource_advisor.contracts import ExecutionResult, signature
from resource_advisor.training import TrainingIsolation, TrainingReceipt, validate_receipt

RUNNER = Path(live_training.__file__)


def make_binding(checkpoint, inputs, **override):
    cp, inp = live_training.encode(checkpoint), live_training.encode(inputs)
    return TrainingIsolation(
        ref="live-train",
        project_ref="team-a",
        workload_ref="live-train",
        workload_digest=signature("workload"),
        checkpoint_digest=signature(checkpoint),
        input_digest=signature(inputs),
        checkpoint_size_bytes=len(cp),
        input_size_bytes=len(inp),
        validation_refs=("live-fixture",),
        **override,
    )


def environment(binding, *, epochs=3, samples=24, features=3):
    context = {
        "allocation_mode": "cpu_only",
        "resources": {"host_cpu": 1, "host_memory_mib": 512, "accelerator_count": 0},
        "parameters": {},
    }
    return {
        "PATH": "/usr/bin:/bin",
        "RA_TRAINING_BINDING": json.dumps(binding.model_dump(mode="json")),
        "RA_CONTEXT_JSON": json.dumps(context),
        "RA_WORK_UNITS": str(epochs),
        "RA_PRECISION": "fp64",
        "RA_INPUT_SHAPE": json.dumps([samples, features]),
        "RA_SEED": "0",
        "RA_JOB_ID": "j-live",
        "RA_ATTEMPT_ID": "a-live",
        "RA_EPOCH": "1",
        "RA_WORKLOAD_SIGNATURE": signature("w"),
        "RA_CONTEXT_SIGNATURE": signature("c"),
    }


def source_tree(tmp_path, checkpoint, inputs):
    source = tmp_path / "source"
    source.mkdir()
    (source / "checkpoint.json").write_bytes(live_training.encode(checkpoint))
    (source / "input.json").write_bytes(live_training.encode(inputs))
    for name in ("checkpoint.json", "input.json"):
        (source / name).chmod(0o444)
    source.chmod(0o555)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    return source, work


def host_run(tmp_path, binding, source, work, **env):
    # -I: the native runtime executes this file without the package on sys.path.
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(RUNNER),
            "--host-source",
            str(source),
            "--host-work",
            str(work),
            "--source-owner-uid",
            str(os.getuid()),
        ],
        env=environment(binding, **env),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def fixture():
    return live_training.fixture(seed=7, samples=24, features=3, hidden=4, lr=0.1, batch_size=8)


def test_host_copy_training_reports_progress_and_validated_checkpoint(tmp_path):
    checkpoint, inputs = fixture()
    binding = make_binding(checkpoint, inputs, backend="slurm", device_class="cpu")
    source, work = source_tree(tmp_path, checkpoint, inputs)
    proc = host_run(tmp_path, binding, source, work)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    progress = [json.loads(x[len("RA_PROGRESS ") :]) for x in lines if x.startswith("RA_PROGRESS ")]
    assert [p["epoch"] for p in progress] == [0, 1, 2, 3]
    assert {p["attempt_id"] for p in progress} == {"a-live"}
    assert [p["optimizer_steps"] for p in progress] == [0, 3, 6, 9]
    assert progress[-1]["loss"] < progress[0]["loss"]
    envelope = json.loads(lines[-1][len("RESOURCE_ADVISOR_RESULT ") :])
    result = ExecutionResult.model_validate(envelope["result"])
    # The standalone encoder must agree with the service's canonical digest.
    assert envelope["digest"] == signature(result)
    receipt = validate_receipt(envelope["training_receipt"], result, binding)
    assert receipt.source_protection == "operator-owned-readonly-host-copy"
    assert receipt.output_checkpoint_digest != binding.checkpoint_digest
    assert receipt.output_checkpoint["step"] == 9
    saved = work / "a-live" / "output" / "checkpoint.json"
    assert signature(json.loads(saved.read_text())) == receipt.output_checkpoint_digest
    assert (source / "checkpoint.json").read_bytes() == live_training.encode(checkpoint)
    assert result.measurements.work_units == 3 and result.measurements.gpu_utilization is None


def test_host_copy_refuses_writable_source_and_reused_sandbox(tmp_path):
    checkpoint, inputs = fixture()
    binding = make_binding(checkpoint, inputs, backend="slurm", device_class="cpu")
    source, work = source_tree(tmp_path, checkpoint, inputs)
    source.chmod(0o755)
    proc = host_run(tmp_path, binding, source, work)
    assert proc.returncode != 0 and "RESOURCE_ADVISOR_RESULT" not in proc.stdout
    source.chmod(0o555)
    (work / "a-live").mkdir()
    proc = host_run(tmp_path, binding, source, work)
    assert proc.returncode != 0 and "RA_PROGRESS" not in proc.stdout


def test_host_copy_requires_slurm_binding(tmp_path):
    checkpoint, inputs = fixture()
    source, work = source_tree(tmp_path, checkpoint, inputs)
    proc = host_run(tmp_path, make_binding(checkpoint, inputs), source, work)
    assert proc.returncode != 0 and not list(work.iterdir())


def test_legacy_gpu_binding_signature_is_unchanged():
    checkpoint, inputs = fixture()
    dumped = make_binding(checkpoint, inputs).model_dump(mode="json")
    assert "device_class" not in dumped and dumped["backend"] == "kubernetes"
    assert make_binding(checkpoint, inputs, device_class="cpu").model_dump()["device_class"] == "cpu"
    with pytest.raises(ValueError):
        make_binding(checkpoint, inputs, backend="slurm")  # GPU Slurm training is unqualified.


def test_receipt_protection_must_match_backend(tmp_path):
    checkpoint, inputs = fixture()
    binding = make_binding(checkpoint, inputs, backend="slurm", device_class="cpu")
    source, work = source_tree(tmp_path, checkpoint, inputs)
    envelope = json.loads(host_run(tmp_path, binding, source, work).stdout.splitlines()[-1][24:])
    result = ExecutionResult.model_validate(envelope["result"])
    receipt = envelope["training_receipt"]
    claimed = dict(receipt, source_not_mounted=True)
    claimed.pop("source_protection")
    with pytest.raises(ValueError):
        validate_receipt(claimed, result, binding)
    container = binding.model_copy(update={"backend": "kubernetes"})
    with pytest.raises(ValueError):
        validate_receipt(receipt, result, container)
    with pytest.raises(ValueError):
        TrainingReceipt.model_validate(dict(receipt, source_not_mounted=True))
    unchanged = dict(
        receipt,
        output_checkpoint=checkpoint,
        output_checkpoint_digest=binding.checkpoint_digest,
    )
    with pytest.raises(ValueError, match="mismatch"):
        validate_receipt(unchanged, result, binding)


def test_kubernetes_cpu_training_manifest_has_no_accelerator_and_isolated_copies(service):
    from resource_advisor.backends import KubernetesBackend
    from resource_advisor.contracts import JobRequest, now
    from resource_advisor.live_training_registry import build

    items = build(
        project="team-a", ref="live-k8s", backend="kubernetes", cluster="lab", node="cpu-node",
        observed_at=now(), valid_for_seconds=3600, capability_evidence=("node-readback",),
        validation_refs=("live-runner-tests",),
        setting={"seed": 2, "samples": 24, "features": 3, "hidden": 4, "lr": 0.1,
                 "batch_size": 8, "epochs": 4},
        resources={"host_cpu": 1, "host_memory_mib": 512}, node_cpu=24, node_memory_mib=8192,
        arch="amd64", environment_digest=signature("image"), runtime_versions={"python": "3.13"},
        command=["python", "-m", "resource_advisor.live_training"],
        image="registry.invalid/ra@" + signature("image"),
    )
    for kind in ("workload", "variant", "capability", "training_isolation"):
        service.register(kind, items[kind], "team-a")
    job = service.submit("team-a", JobRequest(workload_ref="live-k8s", candidate_ref="cpu"), "k")
    with service.store.transaction() as conn:
        row = service.store.job(conn, job["job_id"])
    backend = KubernetesBackend(
        namespace="ra-live", local_queue="live-cpu", node_selector={"pool": "cpu"},
        training_sources={"live-k8s": {
            "binding_digest": signature(items["training_isolation"]),
            "source": {"config_map": "live-k8s-source"},
            "checkpoint_key": "checkpoint.json", "input_key": "input.json",
        }},
    )
    pod = backend.manifest(row)["spec"]["template"]["spec"]
    main = pod["containers"][0]
    assert main["resources"]["requests"] == {"cpu": "1.0", "memory": "512Mi"}
    assert main["command"] == ["python", "-m", "resource_advisor.live_training"]
    assert pod["initContainers"][0]["command"] == ["python", "-m", "resource_advisor.training"]
    assert not any(v["name"] == "training-original" for v in main["volumeMounts"])
