import copy
import hashlib
import json
import sys

import pytest

from resource_advisor import cli
from resource_advisor.configuration import (
    ConfigurationError,
    api_configuration,
    check_configuration,
    read_config,
    worker_configuration,
)


def credentials(*projects):
    return {
        hashlib.sha256(project.encode()).hexdigest(): {"project": project, "operator": False}
        for project in projects
    }


def deployment():
    routes = [
        {
            "project": project,
            "cluster": "kube",
            "backend": "kubernetes",
            "options": {
                "namespace": project,
                "local_queue": "research",
                "node_selector": {"pool": "qualified"},
            },
        }
        for project in ("a", "b")
    ]
    artifacts = {"buckets": {"a": "shared-lab-results", "b": "shared-lab-results"}}
    worker = {
        "routes": routes,
        "artifacts": copy.deepcopy(artifacts),
        "mlflow_url": "http://mlflow.example.invalid",
        "mlflow_experiments": {"a": "1", "b": "2"},
    }
    return credentials("a", "b"), artifacts, worker


def test_multi_project_mapping_and_subset_workers_are_valid():
    identities, artifacts, worker = deployment()
    report = check_configuration(credentials=identities, artifacts=artifacts, worker=worker)
    assert report["api_projects"] == report["worker_projects"] == report["routes"] == 2
    # Separate workers may each serve only part of an API's projects.
    worker["routes"] = worker["routes"][:1]
    assert (
        check_configuration(credentials=identities, artifacts=artifacts, worker=worker)[
            "worker_projects"
        ]
        == 1
    )


@pytest.mark.parametrize("component", ["api", "worker"])
def test_e6_missing_artifact_mapping_is_rejected(component):
    identities, artifacts, worker = deployment()
    del (artifacts if component == "api" else worker["artifacts"])["buckets"]["b"]
    with pytest.raises(ConfigurationError, match="ARTIFACT_PROJECT_MAPPING_MISSING"):
        check_configuration(credentials=identities, artifacts=artifacts, worker=worker)


@pytest.mark.parametrize("mapping", [{}, {"a": "1"}, {"a": "1", "b": ""}, {"a": "1", "b": 2}])
def test_enabled_mlflow_requires_all_worker_project_experiments(mapping):
    _, _, worker = deployment()
    worker["mlflow_experiments"] = mapping
    with pytest.raises(ConfigurationError, match="MLFLOW_PROJECT_MAPPING_MISSING"):
        worker_configuration(worker)


def test_cross_service_bucket_mismatch_and_disabled_reader_rejected():
    identities, artifacts, worker = deployment()
    worker["artifacts"]["buckets"]["b"] = "other-results"
    with pytest.raises(ConfigurationError, match="API_WORKER_BUCKET_MAPPING_MISMATCH"):
        check_configuration(credentials=identities, artifacts=artifacts, worker=worker)
    with pytest.raises(ConfigurationError, match="API_WORKER_ARTIFACT_ENABLEMENT_MISMATCH"):
        check_configuration(credentials=identities, worker=worker)


def test_unreachable_project_identity_is_rejected_without_requiring_api_only_projects_to_run():
    _, artifacts, worker = deployment()
    with pytest.raises(ConfigurationError, match="WORKER_PROJECT_WITHOUT_API_IDENTITY"):
        check_configuration(credentials=credentials("a"), artifacts=artifacts, worker=worker)
    assert (
        check_configuration(credentials=credentials("a", "b", "c"), worker={"routes": []})[
            "api_projects"
        ]
        == 3
    )


def test_optional_integrations_remain_explicitly_disabled():
    report = check_configuration(credentials=credentials("a"), worker={"routes": []})
    assert report["status"] == "PASS"
    assert report["api_artifacts_enabled"] is report["worker_artifacts_enabled"] is False
    assert report["mlflow_enabled"] is False
    assert report["optimizer_required"] is False


@pytest.mark.parametrize("missing", ["torch", "botorch"])
def test_required_optimizer_missing_fails_before_backend_construction(monkeypatch, missing):
    import resource_advisor.configuration as configuration

    original = configuration.importlib.import_module

    def import_module(name):
        if name == missing:
            raise ModuleNotFoundError("private library path must not escape")
        return original(name)

    def forbidden(*args, **kwargs):
        pytest.fail("missing required runtime must not construct a backend")

    monkeypatch.setattr(configuration.importlib, "import_module", import_module)
    monkeypatch.setattr(configuration, "KubernetesBackend", forbidden)
    _, _, worker = deployment()
    worker["optimizer_required"] = True
    with pytest.raises(ConfigurationError, match="^OPTIMIZER_RUNTIME_UNAVAILABLE$"):
        worker_configuration(worker)


def test_explicit_optimizer_requirement_with_installed_runtime():
    pytest.importorskip("torch")
    pytest.importorskip("botorch")
    assert (
        check_configuration(worker={"routes": [], "optimizer_required": True})["optimizer_required"]
        is True
    )


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_optimizer_requirement_rejects_ambiguous_flags(value):
    with pytest.raises(ConfigurationError, match="INVALID_OPTIMIZER_REQUIREMENT"):
        worker_configuration({"routes": [], "optimizer_required": value})


def test_distinct_backend_pools_and_invalid_route_options():
    _, _, worker = deployment()
    worker["routes"].append(
        {
            "project": "a",
            "cluster": "hpc",
            "backend": "slurm",
            "options": {
                "account": "team-a",
                "partition": "gpu",
                "qos": "normal",
                "output_dir": "/private/results",
            },
        }
    )
    assert len(worker_configuration(worker)) == 3
    worker["routes"][-1]["cluster"] = "kube"
    with pytest.raises(ConfigurationError, match="DUPLICATE_WORKER_ROUTE"):
        worker_configuration(worker)
    worker["routes"][-1]["cluster"] = "hpc"
    worker["routes"][-1]["options"]["account"] = "unsafe private value!"
    with pytest.raises(ConfigurationError, match="^INVALID_BACKEND_OPTIONS$"):
        worker_configuration(worker)


@pytest.mark.parametrize("operator", ["false", "true", 0, 1, None])
def test_principal_operator_does_not_accept_truthy_strings_or_numbers(operator):
    value = credentials("a")
    next(iter(value.values()))["operator"] = operator
    with pytest.raises(ConfigurationError, match="INVALID_PRINCIPAL"):
        api_configuration(value)


def test_reader_rejects_duplicate_project_key_without_printing_secret(tmp_path):
    path = tmp_path / "private.json"
    path.write_text('{"private-key":"first-secret","private-key":"second-secret"}')
    with pytest.raises(ConfigurationError, match="^DUPLICATE_JSON_KEY$"):
        read_config(path)
    path.write_text('{"secret":')
    with pytest.raises(ConfigurationError, match="^CONFIG_UNREADABLE_OR_INVALID_JSON$"):
        read_config(path)


@pytest.mark.parametrize("action", ["serve", "worker", "check-config"])
def test_cli_rejects_missing_mapping_before_database_or_remote_construction(
    action, tmp_path, monkeypatch, capsys
):
    identities, artifacts, worker = deployment()
    del artifacts["buckets"]["b"]
    del worker["artifacts"]["buckets"]["b"]
    paths = {}
    for name, value in [("credentials", identities), ("artifacts", artifacts), ("worker", worker)]:
        paths[name] = tmp_path / (name + ".json")
        paths[name].write_text(json.dumps(value))

    def forbidden(*args, **kwargs):
        pytest.fail("invalid configuration must fail before database/network construction")

    monkeypatch.setattr(cli, "Store", forbidden)
    import resource_advisor.configuration as configuration

    monkeypatch.setattr(configuration, "KubernetesBackend", forbidden)
    argv = ["resource-advisor", action]
    if action == "worker":
        argv += ["--config", str(paths["worker"])]
    else:
        argv += [
            "--credentials",
            str(paths["credentials"]),
            "--artifacts-config",
            str(paths["artifacts"]),
        ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    assert "ARTIFACT_PROJECT_MAPPING_MISSING" in capsys.readouterr().err


def test_check_config_success_has_no_database_network_or_secret_output(
    tmp_path, monkeypatch, capsys
):
    import socket

    identities, artifacts, worker = deployment()
    argv = ["resource-advisor", "--database", "invalid-on-purpose", "check-config"]
    for flag, value in [
        ("credentials", identities),
        ("artifacts-config", artifacts),
        ("worker-config", worker),
    ]:
        path = tmp_path / (flag + ".json")
        path.write_text(json.dumps(value))
        argv += ["--" + flag, str(path)]

    def forbidden(*args, **kwargs):
        pytest.fail("configuration check must not connect")

    monkeypatch.setattr(cli, "Store", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    output = capsys.readouterr().out
    assert json.loads(output)["routes"] == 2
    assert "example.invalid" not in output and "shared-lab-results" not in output
    assert all(token_hash not in output for token_hash in identities)


@pytest.mark.parametrize(
    "args,code",
    [
        ({}, "API_OR_WORKER_CONFIG_REQUIRED"),
        ({"worker": {"routes": []}, "artifacts": {}}, "ARTIFACT_CONFIG_REQUIRES_CREDENTIALS"),
    ],
)
def test_incomplete_check_input_rejected(args, code):
    with pytest.raises(ConfigurationError, match=code):
        check_configuration(**args)
