import importlib.util
import json
import shlex
from pathlib import Path

import pytest

from resource_advisor.backends import BackendError, SlurmBackend
from resource_advisor.contracts import JobRequest, now, signature
from resource_advisor.live_training_registry import build

spec = importlib.util.spec_from_file_location(
    "gateway", Path(__file__).parents[1] / "examples/slurm_executor.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)

SOURCE = "/srv/ra/training-sources/live-a"
COMMAND = [
    "/usr/bin/python3", "-I", "/opt/ra/runtime/live_training.py",
    "--host-source", SOURCE, "--host-work", "/srv/ra/work",
]


def registered(service, backend="slurm"):
    items = build(
        project="team-a",
        ref="live-a",
        backend=backend,
        cluster="vm",
        node="ra-vm-node",
        observed_at=now(),
        valid_for_seconds=3600,
        capability_evidence=("sinfo-readback",),
        validation_refs=("live-runner-tests",),
        setting={"seed": 1, "samples": 24, "features": 3, "hidden": 4, "lr": 0.1,
                 "batch_size": 8, "epochs": 5},
        resources={"host_cpu": 2, "host_memory_mib": 1024},
        node_cpu=2,
        node_memory_mib=3072,
        arch="amd64",
        environment_digest=signature("vm-python"),
        runtime_versions={"python": "3.13"},
        command=COMMAND,
        execution={"max_run_seconds": 600, "max_queue_seconds": 900},
    )
    for kind in ("workload", "variant", "capability", "training_isolation"):
        service.register(kind, items[kind], "team-a")
    job = service.submit("team-a", JobRequest(workload_ref="live-a", candidate_ref="cpu"), "k1")
    with service.store.transaction() as conn:
        return items, dict(service.store.job(conn, job["job_id"]))


def runtime(job):
    return {
        job["body"]["variant"]["ref"]: {
            "environment_digest": job["body"]["variant"]["environment_digest"],
            "guard_path": "/opt/ra/runtime/runtime_guard.py",
            "guard_digest": "sha256:" + "1" * 64,
            "manifest_path": "/opt/ra/runtime/runtime.json",
            "manifest_digest": "sha256:" + "2" * 64,
            "commands": [COMMAND],
        }
    }


def backend_for(job, items, **extra):
    return SlurmBackend(
        partition="compute",
        account="ra-project",
        qos="ra-normal",
        output_dir="/srv/ra/results",
        native_runtimes=runtime(job),
        **extra,
    )


def route(items):
    return {
        "live-a": {
            "binding_digest": signature(items["training_isolation"]),
            "source_dir": SOURCE,
            "work_dir": "/srv/ra/work",
        }
    }


def test_slurm_training_fails_closed_without_operator_source_route(service):
    items, job = registered(service)
    with pytest.raises(BackendError, match="checkpoint isolation"):
        backend_for(job, items).script(job)
    changed = route(items)
    changed["live-a"]["source_dir"] = "/srv/ra/training-sources/other"
    with pytest.raises(BackendError):
        backend_for(job, items, training_sources=changed).script(job)


def test_slurm_cpu_training_script_passes_gateway_with_binding(service):
    items, job = registered(service)
    script = backend_for(job, items, training_sources=route(items)).script(job)
    assert "--gres" not in script and "#SBATCH --cpus-per-task=2" in script
    exported = next(x for x in script.splitlines() if x.startswith("export RA_TRAINING_BINDING="))
    binding = json.loads(shlex.split(exported)[1].split("=", 1)[1])
    assert signature(binding) == signature(items["training_isolation"])
    config = {
        "mode": "controller",
        "user": "ra",
        "partition": "compute",
        "account": "ra-project",
        "qos": "ra-normal",
        "node": "ra-vm-node",
        "cpu_only": True,
        "output_dir": "/srv/ra/results",
        "resources": [{"host_cpu": 2, "host_memory_mib": 1024, "accelerator_count": 0}],
        "max_minutes": 10,
        "training_bindings": [signature(items["training_isolation"])],
        "native_bindings": [
            {
                "environment_digest": job["body"]["variant"]["environment_digest"],
                "tail": script[script.index("printf ") :],
            }
        ],
    }
    assert gateway.plan(config, "sbatch --parsable", script)[1] == script
    with pytest.raises(ValueError):
        gateway.plan(dict(config, training_bindings=[]), "sbatch --parsable", script)
    tampered = script.replace(binding["checkpoint_digest"], signature("other"))
    with pytest.raises(ValueError):
        gateway.plan(config, "sbatch --parsable", tampered)
    with pytest.raises(ValueError):
        gateway.plan(dict(config, resources=None), "sbatch --parsable", script)


def test_gateway_allows_live_observation_and_bounded_output_reads():
    config = {
        "mode": "controller", "user": "ra", "account": "ra-project", "partition": "compute",
    }
    command, _ = gateway.plan(
        config,
        "squeue --noheader --account ra-project --partition compute "
        "--format='%i|%j|%a|%P|%T|%r|%N'",
    )
    assert command[0] == "/usr/bin/squeue" and command[-2:] == ["--user", "ra"]
    results = {"mode": "results", "output_dir": "/srv/ra/results"}
    for size, kind in (("16384", "read-progress"), ("65537", "read")):
        assert gateway.plan(results, f"tail -c {size} -- /srv/ra/results/a-1.log") == (
            kind,
            "/srv/ra/results/a-1.log",
        )
    with pytest.raises(ValueError):
        gateway.plan(results, "tail -c 99999 -- /srv/ra/results/a-1.log")


def test_scheduling_plan_admits_qualified_slurm_cpu_training(service):
    from resource_advisor.scheduling import SchedulingPlanRequest, SchedulingProfile, compile_plan

    registered(service)
    service.operational_mode = True
    profile = SchedulingProfile.model_validate(
        {
            "ref": "live-training",
            "project_ref": "team-a",
            "name": "Live training",
            "version": 1,
            "policy": {
                "backend_order": ["slurm"],
                "allocation_modes": ["cpu_only"],
                "max_resources": {"host_cpu": 2, "host_memory_mib": 1024, "accelerator_count": 0},
                "max_run_seconds": 600,
                "max_queue_seconds": 900,
            },
            "bindings": [
                {
                    "backend": "slurm",
                    "cluster_ref": "vm",
                    "partition": "compute",
                    "account": "ra-project",
                    "priority_map": {"normal": "ra-normal"},
                }
            ],
        }
    )
    service.register("scheduling_profile", profile, "team-a")
    with service.store.transaction() as conn:
        plan = compile_plan(
            service,
            conn,
            "team-a",
            SchedulingPlanRequest(profile_ref="live-training", workload_ref="live-a"),
        )
    assert plan["plan"]["backend"] == "slurm", plan


def test_gateway_entrypoint_dispatches_scheduler_commands_and_bounded_reads(
    tmp_path, monkeypatch, capsys
):
    import json as _json
    import os
    import subprocess

    config = tmp_path / "controller.json"
    config.write_text(
        _json.dumps({"mode": "controller", "user": "ra", "account": "ra-project",
                     "partition": "compute"})
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="81|a-1|ra-project|compute\n")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["gateway", "--config", str(config)])
    monkeypatch.setenv(
        "SSH_ORIGINAL_COMMAND",
        "squeue --noheader --account ra-project --partition compute "
        "--format='%i|%j|%a|%P|%T|%r|%N'",
    )
    assert gateway.main() == 0
    assert calls[-1][0] == "/usr/bin/squeue" and calls[-1][-2:] == ["--user", "ra"]
    assert "81|a-1" in capsys.readouterr().out
    log = tmp_path / "results" / "a-1.log"
    log.parent.mkdir()
    log.write_text("x" * 20000 + "\nRA_PROGRESS tail\n")
    config.write_text(_json.dumps({"mode": "results", "output_dir": str(log.parent)}))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"tail -c 16384 -- {log}")
    out = []
    sink = type("Out", (), {"buffer": type("B", (), {"write": staticmethod(out.append)})()})()
    monkeypatch.setattr(gateway.sys, "stdout", sink)
    assert gateway.main() == 0
    assert len(out[0]) == 16384 and out[0].endswith(b"RA_PROGRESS tail\n")
    assert os.getuid() == log.stat().st_uid


def test_slurm_cpu_training_completion_keeps_node_and_zero_accelerators(service):
    items, job = registered(service)
    job["body"]["external_id"] = "9"
    job["body"]["placement"] = {"source": "slurm:squeue", "job_id": "9", "node_name": "ra-vm-node"}
    attempt = job["body"]["attempt_id"]
    calls = []

    def execute(args, **_):
        calls.append(args)
        if args[0] == "squeue":
            return ""
        if args[0] == "ssh":
            return "RA_PROGRESS done\n"
        return (
            f"9|{attempt}|ra-project|compute|COMPLETED|0:0|2026-10-28T01:00:05+0000|"
            "2026-10-28T01:02:05+0000|billing=2,cpu=2,mem=1024M,node=1|2026-10-28T01:00:00+0000\n"
        )

    backend = backend_for(
        job, items, training_sources=route(items), result_ssh_targets={"ra-vm-node": "results"},
        observe_progress=True, observe_allocation=True, execute=execute,
    )
    observation = backend.status(job)
    assert observation.state == "COLLECTING"
    assert observation.allocation["accelerator_count"] == 0.0  # CPU-only AllocTRES has no GRES.
    backend.progress(job, observation)  # Node comes from the saved running placement.
    assert calls[-1][-2:] == ["results", "tail -c 16384 -- /srv/ra/results/" + attempt + ".log"]
