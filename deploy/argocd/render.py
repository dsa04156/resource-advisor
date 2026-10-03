"""Render private, revision-pinned Argo CD Applications for static lab services.

This is an operator tool, not a runtime job submitter. Output contains site image
and node references and must stay outside the public repository. Prerequisite
Secrets, namespaces and cluster-level inventory RBAC are provisioned separately.
"""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo_url: str = "https://github.com/dsa04156/resource-advisor.git"
    revision: str
    argocd_namespace: str = "argocd"
    service_node_selector: dict[str, str]
    postgres_node_selector: dict[str, str]
    images: dict[str, str]
    worker_replicas: int = 0

    @field_validator("repo_url")
    @classmethod
    def repository(cls, value):
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not url.path.endswith(".git")
        ):
            raise ValueError("credential-free HTTPS Git repository required")
        return value

    @field_validator("revision")
    @classmethod
    def revision_pin(cls, value):
        if not re.fullmatch(r"[0-9a-f]{40}", value) or set(value) == {"0"}:
            raise ValueError("full immutable Git commit required")
        return value

    @field_validator("argocd_namespace")
    @classmethod
    def namespace(cls, value):
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", value):
            raise ValueError("explicit Kubernetes namespace required")
        return value

    @field_validator("service_node_selector", "postgres_node_selector")
    @classmethod
    def node_pool(cls, value):
        if not value or any(not k or not v or "*" in k + v for k, v in value.items()):
            raise ValueError("explicit qualified node selector required")
        return value

    @field_validator("images")
    @classmethod
    def image_pins(cls, value):
        required = {"api", "inventory", "worker", "postgres"}
        if not required <= set(value) or set(value) - required - {"slurm-inventory"}:
            raise ValueError("four core images and optional slurm-inventory image required")
        for image in value.values():
            if (
                not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image)
                or "example.invalid" in image
                or image.endswith("0" * 64)
            ):
                raise ValueError("qualified digest-pinned images required")
        return value

    @field_validator("worker_replicas", mode="before")
    @classmethod
    def worker_count(cls, value):
        if type(value) is not int or value not in {0, 1}:
            raise ValueError("worker must be disabled or a single qualified replica")
        return value


def render(site: Site):
    namespace, project = "resource-advisor-lab", "resource-advisor"
    destination = {"server": "https://kubernetes.default.svc", "namespace": namespace}
    project_obj = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "AppProject",
        "metadata": {"name": project, "namespace": site.argocd_namespace},
        "spec": {
            "description": "Static Resource Advisor services; no compute jobs, secrets or cluster RBAC",
            "sourceRepos": [site.repo_url],
            "destinations": [destination],
            "clusterResourceWhitelist": [],
            "namespaceResourceWhitelist": [
                {"group": group, "kind": kind}
                for group, kind in [
                    ("apps", "Deployment"),
                    ("apps", "StatefulSet"),
                    ("", "Service"),
                    ("", "ServiceAccount"),
                    ("rbac.authorization.k8s.io", "Role"),
                    ("rbac.authorization.k8s.io", "RoleBinding"),
                ]
            ],
        },
    }
    apps = []
    groups = [
        ("services", ["api", "inventory"]),
        ("worker", ["worker"]),
        ("postgres", ["postgres"]),
    ]
    if "slurm-inventory" in site.images:
        groups.append(("slurm-inventory", ["slurm-inventory"]))
    for group, components in groups:
        patches = []
        for component in components:
            ops = [
                {
                    "op": "add",
                    "path": "/metadata/annotations",
                    "value": {"resource-advisor.io/gitops-release": site.revision},
                },
                {
                    "op": "replace",
                    "path": "/spec/template/spec/nodeSelector",
                    "value": site.postgres_node_selector
                    if component == "postgres"
                    else site.service_node_selector,
                },
                {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/image",
                    "value": site.images[component],
                },
            ]
            if component == "worker":
                ops.append(
                    {"op": "replace", "path": "/spec/replicas", "value": site.worker_replicas}
                )
            patches.append(
                {
                    "target": {
                        "kind": "StatefulSet" if component == "postgres" else "Deployment",
                        "name": "ra-" + component,
                    },
                    "patch": json.dumps(ops),
                }
            )
        apps.append(
            {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Application",
                "metadata": {
                    "name": "resource-advisor-" + group,
                    "namespace": site.argocd_namespace,
                },
                "spec": {
                    "project": project,
                    "source": {
                        "repoURL": site.repo_url,
                        "targetRevision": site.revision,
                        "path": "deploy/" + group,
                        "kustomize": {"patches": patches},
                    },
                    "destination": destination,
                    # Manual sync protects intentionally stopped workers during lab
                    # failure tests. No prune, force, replace or cascading finalizer.
                    "syncPolicy": {"syncOptions": ["FailOnSharedResource=true"]},
                },
            }
        )
    return {"apiVersion": "v1", "kind": "List", "items": [project_obj, *apps]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    if args.output.resolve().is_relative_to(repo):
        parser.error("private site output must be outside the public checkout")
    result = render(Site.model_validate_json(args.site.read_text()))
    with args.output.open("x") as target:
        args.output.chmod(0o600)
        target.write(json.dumps(result, indent=2) + "\n")
