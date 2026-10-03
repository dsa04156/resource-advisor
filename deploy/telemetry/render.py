"""Render a narrowly scoped Argo application for private lab telemetry targets."""

import argparse
import json
from ipaddress import IPv4Address
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

REPOSITORY = "https://github.com/dsa04156/resource-advisor.git"
NAMESPACE = "resource-advisor-lab"


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_ref: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    address: IPv4Address

    @field_validator("address")
    @classmethod
    def scoped_address(cls, value):
        if not value.is_private or any(
            (value.is_loopback, value.is_link_local, value.is_unspecified, value.is_multicast)
        ):
            raise ValueError("explicit non-loopback lab IPv4 address required")
        return value


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    targets: list[Target] = Field(min_length=1, max_length=32)

    @field_validator("revision")
    @classmethod
    def nonzero_pin(cls, value):
        if set(value) == {"0"}:
            raise ValueError("immutable existing Git revision required")
        return value

    @field_validator("targets")
    @classmethod
    def distinct_targets(cls, value):
        if len({t.node_ref for t in value}) != len(value) or len({t.address for t in value}) != len(
            value
        ):
            raise ValueError("distinct node references and target addresses required")
        return value


def render(site):
    destination = {"server": "https://kubernetes.default.svc", "namespace": NAMESPACE}
    project = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "AppProject",
        "metadata": {"name": "resource-advisor-telemetry", "namespace": "argocd"},
        "spec": {
            "description": "Only lab host scrape configuration; credentials provisioned separately",
            "sourceRepos": [REPOSITORY],
            "destinations": [destination],
            "clusterResourceWhitelist": [],
            "namespaceResourceWhitelist": [
                {"group": "monitoring.coreos.com", "kind": "ScrapeConfig"}
            ],
        },
    }
    patch = [
        {
            "op": "replace",
            "path": "/spec/staticConfigs",
            "value": [
                {
                    "targets": [str(t.address) + ":19100"],
                    "labels": {
                        "node_ref": t.node_ref,
                        "cluster_ref": "lab-slurm",
                        "backend": "slurm",
                    },
                }
                for t in site.targets
            ],
        },
        {
            "op": "add",
            "path": "/metadata/annotations",
            "value": {"resource-advisor.io/gitops-release": site.revision},
        },
    ]
    application = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "resource-advisor-telemetry", "namespace": "argocd"},
        "spec": {
            "project": project["metadata"]["name"],
            "source": {
                "repoURL": REPOSITORY,
                "targetRevision": site.revision,
                "path": "deploy/telemetry",
                "kustomize": {
                    "patches": [
                        {
                            "target": {"kind": "ScrapeConfig", "name": "ra-slurm-hosts"},
                            "patch": json.dumps(patch),
                        }
                    ]
                },
            },
            "destination": destination,
            "syncPolicy": {"syncOptions": ["FailOnSharedResource=true"]},
        },
    }
    return {"apiVersion": "v1", "kind": "List", "items": [project, application]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        parser.error("private site output must be outside the public checkout")
    payload = render(Site.model_validate_json(args.site.read_text()))
    with args.output.open("x") as stream:
        args.output.chmod(0o600)
        stream.write(json.dumps(payload, indent=2) + "\n")
