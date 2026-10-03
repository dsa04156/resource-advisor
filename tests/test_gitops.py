import copy
import json
import runpy
from pathlib import Path

import pytest
from pydantic import ValidationError

MODULE = runpy.run_path(str(Path(__file__).parents[1] / "deploy/argocd/render.py"))
Site, render = MODULE["Site"], MODULE["render"]


def site_config():
    return {
        "revision": "a" * 40,
        "service_node_selector": {"resource-advisor.io/services": "true"},
        "postgres_node_selector": {"resource-advisor.io/metadata": "true"},
        "images": {
            c: "registry.test/" + c + "@sha256:" + "b" * 64
            for c in ["api", "inventory", "worker", "postgres"]
        },
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "main"),
        ("revision", "a" * 7),
        ("revision", "0" * 40),
        ("repo_url", "https://token@github.com/owner/repo.git"),
        ("repo_url", "http://github.com/owner/repo.git"),
        ("service_node_selector", {}),
        ("postgres_node_selector", {"pool": "*"}),
        ("worker_replicas", 2),
        ("worker_replicas", True),
        ("namespace", "another-project"),
        ("syncPolicy", {"automated": {"prune": True}}),
    ],
)
def test_invalid_site_does_not_render_privileged_or_floating_deployment(field, value):
    config = site_config()
    config[field] = value
    with pytest.raises(ValidationError):
        render(Site.model_validate(config))


@pytest.mark.parametrize(
    "image", ["registry.test/api:latest", "registry.test/api@sha256:" + "0" * 64]
)
def test_every_component_requires_qualified_image(image):
    config = site_config()
    config["images"]["api"] = image
    with pytest.raises(ValidationError):
        Site.model_validate(config)


def test_static_sources_exclude_runtime_jobs_secrets_and_cluster_privileges():
    config = site_config()
    before = copy.deepcopy(config)
    project, *apps = render(Site.model_validate(config))["items"]
    assert config == before
    assert project["spec"]["clusterResourceWhitelist"] == []
    allowed = {(r["group"], r["kind"]) for r in project["spec"]["namespaceResourceWhitelist"]}
    for forbidden in [
        ("batch", "Job"),
        ("", "Pod"),
        ("", "Secret"),
        ("", "PersistentVolumeClaim"),
        ("kueue.x-k8s.io", "Workload"),
    ]:
        assert forbidden not in allowed
    assert {a["spec"]["source"]["path"] for a in apps} == {
        "deploy/services",
        "deploy/worker",
        "deploy/postgres",
    }
    for app in apps:
        assert app["spec"]["destination"] == project["spec"]["destinations"][0]
        assert app["spec"]["destination"]["namespace"] == "resource-advisor-lab"
        assert app["spec"]["source"]["targetRevision"] == config["revision"]
        assert "automated" not in app["spec"]["syncPolicy"]
        assert "finalizers" not in app["metadata"]
        assert app["spec"]["syncPolicy"]["syncOptions"] == ["FailOnSharedResource=true"]
    worker = next(a for a in apps if a["metadata"]["name"].endswith("-worker"))
    ops = json.loads(worker["spec"]["source"]["kustomize"]["patches"][0]["patch"])
    assert next(o["value"] for o in ops if o["path"] == "/spec/replicas") == 0


def test_optional_slurm_observer_does_not_expand_project_scope_or_change_core_apps():
    config = site_config()
    original = render(Site.model_validate(config))["items"]
    image = "registry.test/services@sha256:" + "c" * 64
    config["images"]["slurm-inventory"] = image
    extended = render(Site.model_validate(config))["items"]
    assert extended[:-1] == original
    app = extended[-1]
    assert app["metadata"]["name"] == "resource-advisor-slurm-inventory"
    assert app["spec"]["source"]["path"] == "deploy/slurm-inventory"
    assert "automated" not in app["spec"]["syncPolicy"]
    patch = app["spec"]["source"]["kustomize"]["patches"][0]
    assert patch["target"] == {"kind": "Deployment", "name": "ra-slurm-inventory"}
    assert (
        next(
            op["value"]
            for op in json.loads(patch["patch"])
            if op["path"] == "/spec/template/spec/containers/0/image"
        )
        == image
    )
    config["images"]["slurm-inventory"] = "registry.test/services:latest"
    with pytest.raises(ValidationError):
        Site.model_validate(config)
