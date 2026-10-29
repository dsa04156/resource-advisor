import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest
from test_backends import native_runtime, row

from resource_advisor.backends import BackendError, SlurmBackend

spec = importlib.util.spec_from_file_location(
    "gateway", Path(__file__).parents[1] / "examples/slurm_executor.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def test_real_adapter_script_is_scoped_and_injected_values_remain_literal(service):
    job = row(service)
    job["body"]["candidate"]["context"]["resources"]["host_memory_mib"] = 1024
    job["body"]["capability"]["resource_key"] = "gpu:orin_nano"
    job["body"]["spec"]["execution"]["max_run_seconds"] = 120
    job["body"]["execution_limits"]["max_run_seconds"] = 120
    job["body"]["artifact_prefix"] = "$(touch /tmp/never-execute); not-shell"
    backend = SlurmBackend(
        partition="compute",
        account="lab",
        qos="normal",
        output_dir="/opt/results",
        native_runtimes=native_runtime(job),
    )
    script = backend.script(job)
    config = {
        "mode": "controller",
        "user": "executor",
        "partition": "compute",
        "account": "lab",
        "qos": "normal",
        "node": job["body"]["capability"]["node_ref"],
        "gres": "gpu:orin_nano",
        "output_dir": "/opt/results",
        "native_bindings": [
            {
                "environment_digest": job["body"]["variant"]["environment_digest"],
                "tail": script[script.index("printf ") :],
            }
        ],
    }
    argv, canonical = gateway.plan(config, "sbatch --parsable", script)
    assert argv == ["/usr/bin/sbatch", "--parsable"]
    assert canonical == script
    for bad in [
        script + "id\n",
        script.replace("--account=lab", "--account=foreign"),
        script.replace("--mem=1024M", "--mem=2048M"),
        script.replace("--time=2", "--time=3"),
    ]:
        with pytest.raises(ValueError):
            gateway.plan(config, "sbatch --parsable", bad)
    with pytest.raises(ValueError, match="not enabled"):
        gateway.plan(dict(config, native_bindings=[]), "sbatch --parsable", script)

    high = script.replace("--qos=normal", "--qos=research-high")
    with pytest.raises(ValueError, match="QOS outside"):
        gateway.plan(config, "sbatch --parsable", high)
    mapped = dict(config, qos_by_priority={"normal": "normal", "high": "research-high"})
    assert gateway.plan(mapped, "sbatch --parsable", high)[1] == high
    assert gateway.plan(mapped, "sbatch --parsable", script)[1] == script
    with pytest.raises(ValueError, match="QOS outside"):
        gateway.plan(mapped, "sbatch --parsable", script.replace("--qos=normal", "--qos=foreign"))
    for invalid in [
        {"normal": "different-default"},
        {"urgent": "research-high"},
        {"high": "research-high; id"},
        {"high": None},
    ]:
        with pytest.raises(ValueError, match="priority QOS mapping"):
            gateway.plan(dict(config, qos_by_priority=invalid), "sbatch --parsable", script)


def test_adapter_queries_and_cancel_are_forced_to_linux_owner(service):
    job = row(service)
    job["body"]["external_id"] = "36"
    seen = []
    config = {"mode": "controller", "user": "executor", "account": "lab", "partition": "compute"}

    def execute(argv, **_):
        command, data = gateway.plan(config, shlex.join(argv))
        assert command[-2:] == ["--user", "executor"] and data is None
        seen.append(command[0])
        return ""

    backend = SlurmBackend(
        partition="compute", account="lab", qos="normal", output_dir="/opt/results", execute=execute
    )
    assert backend.reconcile(job) is None
    with pytest.raises(BackendError):
        backend.status(job)
    backend.cancel(job)
    # Empty accounting is correctly an observation failure, not a terminal job.
    with pytest.raises(BackendError):
        backend.result(job)
    assert set(seen) == {"/usr/bin/squeue", "/usr/bin/sacct", "/usr/bin/scancel"}


def test_arbitrary_shell_and_expanded_query_scope_are_rejected():
    config = {"mode": "controller", "user": "executor", "account": "lab", "partition": "compute"}
    for command in [
        "id",
        "bash",
        "srun id",
        "sacct --allusers",
        "scontrol update NodeName=n State=DOWN",
        "squeue --noheader --account foreign --partition compute --format=%i|%j|%a|%P|%T",
        "scancel --ctld --account lab --partition compute --name a 1; id",
    ]:
        with pytest.raises(ValueError):
            gateway.plan(config, command)


def test_result_scope_is_separate_from_controller_commands():
    config = {"mode": "results", "output_dir": "/opt/results"}
    assert gateway.plan(config, "tail -c 65537 -- /opt/results/attempt-1.log") == (
        "read",
        "/opt/results/attempt-1.log",
    )
    for command in [
        "tail -c 65537 -- /etc/passwd",
        "tail -c 65537 -- /opt/results/../secret.log",
        "sbatch --parsable",
        "tail -c 999999 -- /opt/results/attempt-1.log",
    ]:
        with pytest.raises(ValueError):
            gateway.plan(config, command)


def test_explicit_cpu_gateway_requires_zero_gres_and_qualified_runtime(service):
    job = row(service)
    body = job["body"]
    body["candidate"]["context"].update(allocation_mode="cpu_only", memory_model="host")
    body["candidate"]["context"]["resources"].update(
        host_cpu=1, host_memory_mib=1024, accelerator_count=0
    )
    body["capability"].update(device_class="cpu", resource_key=None)
    body["variant"]["device_class"] = "cpu"
    body["execution_limits"]["max_run_seconds"] = 120
    backend = SlurmBackend(
        partition="compute",
        account="lab",
        qos="normal",
        output_dir="/opt/results",
        native_runtimes=native_runtime(job),
    )
    script = backend.script(job)
    config = {
        "mode": "controller",
        "user": "executor",
        "partition": "compute",
        "account": "lab",
        "qos": "normal",
        "node": body["capability"]["node_ref"],
        "cpu_only": True,
        "output_dir": "/opt/results",
        "native_bindings": [
            {
                "environment_digest": body["variant"]["environment_digest"],
                "tail": script[script.index("printf ") :],
            }
        ],
    }
    assert "#SBATCH --gres" not in script
    assert gateway.plan(config, "sbatch --parsable", script)[1] == script
    # CPU permission is not a fallback or a grant to alter resource bounds.
    for bad in [
        script.replace("#SBATCH --export=NONE", "#SBATCH --export=NONE\n#SBATCH --gres=gpu:1"),
        script.replace("--cpus-per-task=1", "--cpus-per-task=2"),
        script.replace("--mem=1024M", "--mem=2048M"),
        script.replace('"accelerator_count":0', '"accelerator_count":1'),
        script.replace('"allocation_mode":"cpu_only"', '"allocation_mode":"physical_device"'),
    ]:
        assert bad != script
        with pytest.raises(ValueError):
            gateway.plan(config, "sbatch --parsable", bad)
    for invalid in [
        dict(config, cpu_only=False, gres="gpu:orin_nano"),
        dict(config, cpu_only="true"),
        dict(config, gres="gpu:orin_nano"),
        dict(config, native_bindings=[]),
    ]:
        with pytest.raises(ValueError):
            gateway.plan(invalid, "sbatch --parsable", script)


def test_containerized_controller_wraps_only_the_already_scoped_argv(service):
    job = row(service)
    job["body"]["external_id"] = "36"
    config = {
        "mode": "controller",
        "user": "slurm",
        "account": "ra-lab",
        "partition": "all",
        "container": "slurmctld",
    }
    seen = []

    def execute(argv, **_):
        command, data = gateway.plan(config, shlex.join(argv))
        assert command[:3] == ["/usr/bin/docker", "exec", "--user"] and data is None
        assert command[3:10] == [
            "slurm",
            "--env",
            "TZ=UTC",
            "--env",
            "SLURM_TIME_FORMAT=%Y-%m-%dT%H:%M:%S%z",
            "slurmctld",
            command[9],
        ]
        assert command[-2:] == ["--user", "slurm"]
        seen.append(command[9])
        return ""

    backend = SlurmBackend(
        partition="all", account="ra-lab", qos="normal", output_dir="/jobs/ra-lab", execute=execute
    )
    assert backend.reconcile(job) is None
    backend.cancel(job)
    assert set(seen) == {"/usr/bin/squeue", "/usr/bin/sacct", "/usr/bin/scancel"}
    for command in ["docker ps", "id", "sinfo", "sacct --allusers"]:
        with pytest.raises(ValueError):
            gateway.plan(config, command)
    for invalid in [
        dict(config, container="slurmctld; id"),
        dict(config, container="-it"),
        dict(config, container=["slurmctld"]),
        dict(config, user="--privileged"),
    ]:
        with pytest.raises(ValueError, match="container or user"):
            gateway.plan(
                invalid,
                "squeue --noheader --account ra-lab --partition all --format=%i|%j|%a|%P|%T",
            )


def test_containerized_submit_without_enabled_runtime_is_rejected_before_docker(service):
    job = row(service)
    body = job["body"]
    body["candidate"]["context"].update(allocation_mode="cpu_only", memory_model="host")
    body["candidate"]["context"]["resources"].update(
        host_cpu=1, host_memory_mib=1024, accelerator_count=0
    )
    body["capability"].update(device_class="cpu", resource_key=None)
    body["variant"]["device_class"] = "cpu"
    body["execution_limits"]["max_run_seconds"] = 120
    backend = SlurmBackend(
        partition="all",
        account="ra-lab",
        qos="normal",
        output_dir="/jobs/ra-lab",
        native_runtimes=native_runtime(job),
    )
    script = backend.script(job)
    config = {
        "mode": "controller",
        "user": "slurm",
        "partition": "all",
        "account": "ra-lab",
        "qos": "normal",
        "node": body["capability"]["node_ref"],
        "cpu_only": True,
        "output_dir": "/jobs/ra-lab",
        "container": "slurmctld",
        "native_bindings": [],
        "training_bindings": [],
    }
    with pytest.raises(ValueError, match="not enabled"):
        gateway.plan(config, "sbatch --parsable", script)
    enabled = dict(
        config,
        native_bindings=[
            {
                "environment_digest": body["variant"]["environment_digest"],
                "tail": script[script.index("printf ") :],
            }
        ],
    )
    argv, canonical = gateway.plan(enabled, "sbatch --parsable", script)
    assert argv[:3] == ["/usr/bin/docker", "exec", "--interactive"]
    assert argv[-3:] == ["slurmctld", "/usr/bin/sbatch", "--parsable"] and canonical == script


def test_containerized_results_map_to_the_configured_host_directory():
    config = {
        "mode": "results",
        "output_dir": "/jobs/ra-lab",
        "host_output_dir": "/home/etri/slurm-docker/jobs/ra-lab",
        "result_owner_uid": 64030,
    }
    assert gateway.plan(config, "tail -c 16384 -- /jobs/ra-lab/attempt-1.log") == (
        "read-progress",
        "/home/etri/slurm-docker/jobs/ra-lab/attempt-1.log",
    )
    for command in [
        "tail -c 65537 -- /home/etri/slurm-docker/jobs/ra-lab/attempt-1.log",
        "tail -c 65537 -- /jobs/attempt-1.log",
        "tail -c 65537 -- /jobs/ra-lab/../x.log",
    ]:
        with pytest.raises(ValueError):
            gateway.plan(config, command)
    with pytest.raises(ValueError, match="host output"):
        gateway.plan(
            dict(config, host_output_dir="relative"), "tail -c 65537 -- /jobs/ra-lab/a.log"
        )


def test_result_owner_is_operator_configured(tmp_path, monkeypatch, capsys):
    log = tmp_path / "attempt-1.log"
    log.write_text("done\n")
    for owner, code in [(log.stat().st_uid, 0), (log.stat().st_uid + 1, 1), ("0", 1)]:
        config = tmp_path / "results.json"
        config.write_text(
            json.dumps(
                {
                    "mode": "results",
                    "output_dir": "/jobs/ra-lab",
                    "host_output_dir": str(tmp_path),
                    "result_owner_uid": owner,
                }
            )
        )
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "tail -c 65537 -- /jobs/ra-lab/attempt-1.log")
        monkeypatch.setattr(sys, "argv", ["gateway", "--config", str(config)])
        assert gateway.main() == code
    assert "done" in capsys.readouterr().out


def test_committed_virtual_140_route_and_gateway_cannot_submit(service):
    from resource_advisor.configuration import worker_configuration

    deploy = Path(__file__).parents[1] / "deploy/slurm-execution-virtual-140"
    routes = json.loads((deploy / "worker-route.example.json").read_text())
    backend = worker_configuration(routes)[("ra-lab", "virtual-slurm-140")]
    job = row(service)
    runtimes = native_runtime(job)
    # No qualified native runtime: the worker refuses before any transport call.
    with pytest.raises(BackendError, match="qualified native runtime"):
        backend.validate(job)
    gpu = SlurmBackend(
        partition="all",
        account="ra-lab",
        qos="normal",
        output_dir="/jobs/ra-lab",
        native_runtimes=runtimes,
    )
    job["body"]["capability"].update(node_ref="gpu-big", resource_key="gpu:a100")
    job["body"]["candidate"]["context"]["resources"].update(host_cpu=1, host_memory_mib=1024)
    job["body"]["execution_limits"]["max_run_seconds"] = 120
    script = gpu.script(job)
    config = json.loads((deploy / "executor.json").read_text())
    assert config["native_bindings"] == [] and config["training_bindings"] == []
    with pytest.raises(ValueError, match="not enabled"):
        gateway.plan(config, "sbatch --parsable", script)
    results = json.loads((deploy / "results.json").read_text())
    assert gateway.plan(results, "tail -c 65537 -- /jobs/ra-lab/a-1.log")[1] == (
        "/home/etri/slurm-docker/jobs/ra-lab/a-1.log"
    )
