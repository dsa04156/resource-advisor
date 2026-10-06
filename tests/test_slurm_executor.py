import importlib.util
import shlex
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
