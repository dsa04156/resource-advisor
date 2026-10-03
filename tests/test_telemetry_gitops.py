import json
import runpy
from pathlib import Path

import pytest
from pydantic import ValidationError

MODULE = runpy.run_path(str(Path(__file__).parents[1] / "deploy/telemetry/render.py"))
Site, render = MODULE["Site"], MODULE["render"]


def config():
    return {"revision": "a" * 40, "targets": [{"node_ref": "lab-a", "address": "192.0.2.2"}]}


@pytest.mark.parametrize(
    "address", ["0.0.0.0", "127.0.0.1", "169.254.1.1", "8.8.8.8", "::1", "host;cmd"]
)
def test_unsafe_or_unqualified_target_rejected(address):
    c = config()
    c["targets"][0]["address"] = address
    with pytest.raises(ValidationError):
        Site.model_validate(c)


@pytest.mark.parametrize(
    "field,value",
    [("revision", "main"), ("revision", "0" * 40), ("namespace", "kube-system"), ("targets", [])],
)
def test_scope_or_revision_bypass_rejected(field, value):
    c = config()
    c[field] = value
    with pytest.raises(ValidationError):
        Site.model_validate(c)


def test_duplicate_node_identity_cannot_double_count_host():
    c = config()
    c["targets"].append({"node_ref": "lab-a", "address": "192.0.2.3"})
    with pytest.raises(ValidationError):
        Site.model_validate(c)


def test_only_scrapeconfig_is_managed_and_no_secret_or_workload_permission():
    c = config()
    project, app = render(Site.model_validate(c))["items"]
    assert project["spec"]["clusterResourceWhitelist"] == []
    assert project["spec"]["namespaceResourceWhitelist"] == [
        {"group": "monitoring.coreos.com", "kind": "ScrapeConfig"}
    ]
    assert app["spec"]["destination"] == project["spec"]["destinations"][0]
    assert app["spec"]["destination"]["namespace"] == "resource-advisor-lab"
    assert app["spec"]["source"]["targetRevision"] == c["revision"]
    assert "automated" not in app["spec"]["syncPolicy"]
    assert "finalizers" not in app["metadata"]
    ops = json.loads(app["spec"]["source"]["kustomize"]["patches"][0]["patch"])
    assert ops[0]["value"][0] == {
        "targets": ["192.0.2.2:19100"],
        "labels": {"node_ref": "lab-a", "cluster_ref": "lab-slurm", "backend": "slurm"},
    }
