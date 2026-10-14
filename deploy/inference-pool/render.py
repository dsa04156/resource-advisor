"""Render a dedicated, pinned GPU/Hailo serving pair from private site values."""

import argparse
import json
import re
from pathlib import Path


def render(site):
    namespace = site["namespace"]
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", namespace):
        raise ValueError("invalid namespace")
    items = []
    for route in site["routes"]:
        name, runtime, image = route["name"], route["runtime"], route["image"]
        if (
            not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", name)
            or runtime not in {"cuda", "hailo"}
            or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image)
            or not route["node"]
        ):
            raise ValueError("explicit runtime, hostname and image digest required")
        labels = {
            "app.kubernetes.io/name": name,
            "app.kubernetes.io/part-of": "resource-advisor",
            "resource-advisor/component": "inference-worker",
        }
        runtime_env = route.get("runtime_env", [])
        if any(
            set(env) != {"name", "value"} or env["name"] not in {"PYTHONPATH", "LD_LIBRARY_PATH"}
            for env in runtime_env
        ) or len({env["name"] for env in runtime_env}) != len(runtime_env):
            raise ValueError("only explicit CUDA library search environment is accepted")
        resources = {
            "cpu": "1",
            "memory": "3Gi" if runtime == "cuda" else "1Gi",
            "nvidia.com/gpu" if runtime == "cuda" else "hailo.ai/h8": "1",
        }
        container = {
            "name": "inference",
            "image": image,
            "command": [
                "python",
                "-u",
                "/opt/inference/runtime.py",
                "--config",
                "/opt/inference/model.json",
            ],
            "env": [
                {"name": "HOME", "value": "/tmp"},
                {"name": "HAILORT_LOGGER_PATH", "value": "/tmp"},
                {"name": "RA_RUNTIME_IMAGE_DIGEST", "value": image.split("@")[1]},
            ]
            + runtime_env,
            "resources": {"requests": resources, "limits": resources},
            "ports": [{"name": "http", "containerPort": 8080}],
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
            "startupProbe": {
                "httpGet": {"path": "/health", "port": "http"},
                "periodSeconds": 2,
                "timeoutSeconds": 2,
                "failureThreshold": 90,
            },
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": "http"},
                "periodSeconds": 2,
                "timeoutSeconds": 2,
            },
        }
        container["livenessProbe"] = {
            "httpGet": {"path": "/health", "port": "http"},
            "periodSeconds": 5,
            "timeoutSeconds": 2,
            "failureThreshold": 3,
        }
        items.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": namespace, "labels": labels},
                "spec": {
                    "replicas": 1,
                    "strategy": {"type": "Recreate"},
                    "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                    "template": {
                        "metadata": {
                            "labels": labels,
                            "annotations": {"sidecar.istio.io/inject": "false"},
                        },
                        "spec": {
                            "automountServiceAccountToken": False,
                            "nodeSelector": {
                                "kubernetes.io/hostname": route["node"],
                                "kubernetes.io/arch": "amd64" if runtime == "cuda" else "arm64",
                            },
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 10001,
                                "runAsGroup": 10001,
                                "fsGroup": 10001,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [container],
                            "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}],
                        },
                    },
                },
            }
        )
        items.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": namespace, "labels": labels},
                "spec": {
                    "selector": {"app.kubernetes.io/name": name},
                    "ports": [{"name": "http", "port": 8080, "targetPort": "http"}],
                },
            }
        )
    if len(items) != 4 or len({r["name"] for r in site["routes"]}) != 2:
        raise ValueError("exactly two distinct runtime routes required")
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = render(json.loads(args.site.read_text()))
    with args.output.open("x") as file:
        file.write(json.dumps(output, indent=2) + "\n")
    args.output.chmod(0o600)


if __name__ == "__main__":
    main()
