"""Forced-command contract tests with scheduler-output doubles, not hardware evidence."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "observer", Path(__file__).parents[1] / "examples/slurm_observer.py"
)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
SCOPE = {"account": "team-a", "partition": "gpu", "nodes": ["worker-a"]}


@pytest.mark.parametrize(
    "command",
    [
        "",
        "sbatch /tmp/job",
        "scancel 1",
        "scontrol update NodeName=worker-a State=DOWN",
        "squeue --json=v0.0.42 --account=team-b --partition=gpu",
        "squeue --json=v0.0.42 --account=team-a --partition=other",
        "scontrol --json=v0.0.42 show nodes; id",
        "scontrol --json=v0.0.42 show $(id)",
        "scontrol --json=v0.0.42 show nodes --config=/tmp/file",
        None,
    ],
)
def test_mutating_foreign_and_shell_commands_never_execute(command):
    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized command executed")

    with pytest.raises(ValueError):
        observer.observe(SCOPE, command, execute=forbidden)


def response(collection, rows):
    return {
        "meta": {
            "plugin": {"data_parser": "data_parser/v0.0.42"},
            "slurm": {"release": "24.11.5"},
            "client": {"user": "private-user"},
        },
        "errors": [],
        "warnings": [],
        collection: rows,
    }


def test_nodes_are_filtered_and_private_attributes_removed():
    payload = response(
        "nodes",
        [
            {
                "name": "worker-a",
                "cpus": 4,
                "address": "private-address",
                "reason": "private-reason",
            },
            {"name": "worker-b", "cpus": 8},
        ],
    )

    def execute(args, **kwargs):
        assert args == ["/usr/bin/scontrol", "--json=v0.0.42", "show", "nodes"]
        assert "SLURM_CONF" not in kwargs["env"] and kwargs["check"]
        return SimpleNamespace(stdout=json.dumps(payload), stderr="")

    result = observer.observe(SCOPE, "scontrol --json=v0.0.42 show nodes", execute=execute)
    assert result["nodes"] == [{"name": "worker-a", "cpus": 4}]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "fault", ["foreign", "parser", "release", "stderr", "duplicate", "missing"]
)
def test_foreign_or_unqualified_response_rejected(fault):
    payload = response("nodes", [{"name": "worker-a"}])
    if fault == "foreign":
        payload = response("jobs", [{"account": "team-b", "partition": "gpu"}])
    elif fault == "parser":
        payload["meta"]["plugin"]["data_parser"] = "other"
    elif fault == "release":
        payload["meta"]["slurm"]["release"] = "25.05.0"
    elif fault == "duplicate":
        payload["nodes"] *= 2
    elif fault == "missing":
        payload["nodes"] = []
    command = (
        "squeue --json=v0.0.42 --account=team-a --partition=gpu"
        if fault == "foreign"
        else "scontrol --json=v0.0.42 show nodes"
    )
    with pytest.raises(ValueError):
        observer.observe(
            SCOPE,
            command,
            execute=lambda *a, **kw: SimpleNamespace(
                stdout=json.dumps(payload), stderr="failure" if fault == "stderr" else ""
            ),
        )


def test_queue_keeps_empty_warning_and_only_required_fields():
    payload = response("jobs", [])
    payload["warnings"] = [{"description": "Zero jobs to dump", "source": ""}]
    result = observer.observe(
        SCOPE,
        "squeue --json=v0.0.42 --account=team-a --partition=gpu",
        execute=lambda *a, **kw: SimpleNamespace(stdout=json.dumps(payload), stderr=""),
    )
    assert result["jobs"] == [] and result["warnings"] == payload["warnings"]
