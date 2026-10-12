"""Namespace-scoped KServe v0.15 RawDeployment adapter with fenced writes."""

import json
import math
import re
import ssl
from pathlib import Path

import httpx
from pydantic import Field, model_validator

from .contracts import Contract, now, signature
from .model_predictor import verified_package

MANAGED = "resource-advisor-models"
REVISION = "resource-advisor.io/model-revision"
INTENT = "resource-advisor.io/model-deployment"


class ServingPending(ValueError):
    pass


class ProjectedTokenAuth(httpx.Auth):
    def __init__(self, path):
        self.path = path

    def auth_flow(self, request):
        request.headers["Authorization"] = "Bearer " + self.path.read_text().strip()
        yield request


class ServingConfig(Contract):
    project_namespaces: dict[str, str] = Field(min_length=1)
    image: str = Field(pattern=r"^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$")
    node_selector: dict[str, str]
    service_account: str = "ra-predictor"

    @model_validator(mode="after")
    def valid_names(self):
        names = [*self.project_namespaces.values(), self.service_account]
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name) for name in names):
            raise ValueError("serving namespaces/accounts must be DNS labels")
        if any(len(name) > 25 for name in self.project_namespaces.values()):
            raise ValueError(
                "serving namespace must fit the default KServe hostname (max 25 characters)"
            )
        return self


def contains_requested(actual, expected):
    """Permit API-defaulted map keys without ignoring the requested pod contract."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and contains_requested(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(contains_requested(a, b) for a, b in zip(actual, expected, strict=True))
        )
    return actual == expected


class KServeAdapter:
    def __init__(self, config, *, client=None, prediction_client=None):
        self.config = config
        if client is None:
            token_dir = Path("/var/run/secrets/kubernetes.io/serviceaccount")
            client = httpx.Client(
                base_url="https://kubernetes.default.svc",
                verify=ssl.create_default_context(cafile=str(token_dir / "ca.crt")),
                auth=ProjectedTokenAuth(token_dir / "token"),
                timeout=10,
            )
        self.client = client
        self.prediction_client = prediction_client or httpx.Client(timeout=10)

    def get(self, path):
        response = self.client.get(path)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def owned(self, obj, project):
        labels = obj["metadata"].get("labels", {})
        if (
            labels.get("app.kubernetes.io/managed-by") != MANAGED
            or labels.get("resource-advisor.io/project") != signature(project)[7:39]
        ):
            raise ServingPending("FOREIGN_SERVING_RESOURCE")

    def predictor_spec(self, cm_name):
        return {
            "minReplicas": 1,
            "maxReplicas": 1,
            "serviceAccountName": self.config.service_account,
            "automountServiceAccountToken": False,
            "nodeSelector": self.config.node_selector,
            "annotations": {"sidecar.istio.io/inject": "false"},
            "deploymentStrategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
            },
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "kserve-container",
                    "image": self.config.image,
                    "command": ["python", "-m", "resource_advisor.model_predictor"],
                    "env": [
                        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                        {"name": "PROTOCOL", "value": "v1"},
                    ],
                    "ports": [{"name": "http1", "containerPort": 8080, "protocol": "TCP"}],
                    "readinessProbe": {
                        "httpGet": {"path": "/healthz", "port": 8080},
                        "timeoutSeconds": 2,
                        "periodSeconds": 5,
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "seccompProfile": {"type": "RuntimeDefault"},
                        "procMount": "Default",
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "1", "memory": "256Mi"},
                    },
                    "volumeMounts": [{"name": "model", "mountPath": "/model", "readOnly": True}],
                }
            ],
            "volumes": [{"name": "model", "configMap": {"name": cm_name}}],
        }

    def apply(self, project, intent, package):
        description, _ = verified_package(package)
        namespace = self.config.project_namespaces[project]
        name = description.service_name
        labels = {
            "app.kubernetes.io/managed-by": MANAGED,
            "resource-advisor.io/project": signature(project)[7:39],
        }
        cm_name = "ra-model-package-" + signature(package)[7:47]
        cm_base = f"/api/v1/namespaces/{namespace}/configmaps"
        data = {"package.json": json.dumps(package, sort_keys=True, separators=(",", ":"))}
        cm = self.get(cm_base + "/" + cm_name)
        if cm is None:
            response = self.client.post(
                cm_base,
                json={
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": cm_name, "namespace": namespace, "labels": labels},
                    "immutable": True,
                    "data": data,
                },
            )
            if response.status_code != 409:
                response.raise_for_status()
            cm = self.get(cm_base + "/" + cm_name)
        self.owned(cm, project)
        if not cm.get("immutable") or cm.get("data") != data:
            raise ServingPending("MODEL_CONFIGMAP_INTEGRITY_MISMATCH")
        base = f"/apis/serving.kserve.io/v1beta1/namespaces/{namespace}/inferenceservices"
        current = self.get(base + "/" + name)
        revision = intent["body"]["revision"]
        annotations = {
            "serving.kserve.io/deploymentMode": "RawDeployment",
            "serving.kserve.io/autoscalerClass": "external",
            REVISION: str(revision),
            INTENT: intent["ref"],
        }
        obj = {
            "apiVersion": "serving.kserve.io/v1beta1",
            "kind": "InferenceService",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {**labels, "networking.kserve.io/visibility": "cluster-local"},
                "annotations": annotations,
            },
            "spec": {"predictor": self.predictor_spec(cm_name)},
        }
        if current:
            self.owned(current, project)
            old_annotations = current["metadata"].get("annotations", {})
            old_revision = int(old_annotations.get(REVISION, "0"))
            if old_revision > revision or (
                old_revision == revision and old_annotations.get(INTENT) != intent["ref"]
            ):
                raise ServingPending("STALE_SERVING_WRITE")
            if old_annotations.get(INTENT) == intent["ref"] and contains_requested(
                current["spec"], obj["spec"]
            ):
                return current, cm_name
            obj["metadata"] = {
                **current["metadata"],
                "labels": {**current["metadata"].get("labels", {}), **obj["metadata"]["labels"]},
                "annotations": {**old_annotations, **annotations},
            }
            response = self.client.put(base + "/" + name, json=obj)
        else:
            response = self.client.post(base, json=obj)
        response.raise_for_status()
        return response.json(), cm_name

    def verify(self, project, intent, package, isvc, cm_name, evaluation, old_uid=None):
        namespace = self.config.project_namespaces[project]
        description = package["description"]
        name = description["service_name"]
        if old_uid and isvc["metadata"]["uid"] != old_uid:
            raise ServingPending("SERVING_RESOURCE_RECREATED")
        deployment = self.get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}-predictor")
        service = self.get(f"/api/v1/namespaces/{namespace}/services/{name}-predictor")
        for resource in (deployment, service):
            if not resource or not any(
                o.get("controller")
                and o.get("uid") == isvc["metadata"]["uid"]
                and o.get("kind") == "InferenceService"
                for o in resource["metadata"].get("ownerReferences", [])
            ):
                raise ServingPending("ROLLOUT_NOT_COMPLETE")
        spec, status = deployment["spec"], deployment.get("status", {})
        if (
            spec.get("replicas") != 1
            or status.get("observedGeneration", 0) < deployment["metadata"]["generation"]
            or status.get("terminatingReplicas", 0)
            or any(
                status.get(k) != 1
                for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
            )
        ):
            raise ServingPending("ROLLOUT_NOT_COMPLETE")
        template = spec["template"]["spec"]
        predictor = self.predictor_spec(cm_name)
        if not contains_requested(isvc["spec"]["predictor"], predictor):
            raise ServingPending("PREDICTOR_TEMPLATE_MISMATCH")
        expected = {
            k: predictor[k]
            for k in (
                "serviceAccountName",
                "automountServiceAccountToken",
                "nodeSelector",
                "securityContext",
                "containers",
                "volumes",
            )
        }
        if (
            not contains_requested(template, expected)
            or template.get("initContainers")
            or any(template.get(k, False) for k in ("hostNetwork", "hostPID", "hostIPC"))
        ):
            raise ServingPending("PREDICTOR_TEMPLATE_MISMATCH")
        container = template["containers"][0]
        requested = expected["containers"][0]
        security = container.get("securityContext", {})
        if (
            any(container.get(k) != requested.get(k) for k in ("args", "envFrom"))
            or security.get("privileged", False)
            or security.get("capabilities", {}).get("add")
            or any(
                security.get(k, expected["securityContext"][k]) != expected["securityContext"][k]
                for k in ("runAsUser", "runAsGroup", "runAsNonRoot")
            )
            or any(
                set(container["resources"][k]) != set(requested["resources"][k])
                for k in ("requests", "limits")
            )
        ):
            raise ServingPending("PREDICTOR_TEMPLATE_MISMATCH")
        if service["spec"].get("type") != "ClusterIP" or not any(
            p["port"] == 80 and p["targetPort"] == 8080 for p in service["spec"].get("ports", [])
        ):
            raise ServingPending("SERVING_SERVICE_MISMATCH")
        endpoint = f"http://{name}-predictor.{namespace}.svc/v1/models/{name}"
        response = self.prediction_client.get(endpoint)
        response.raise_for_status()
        if not response.json().get("ready") or response.json().get("model") != description:
            raise ServingPending("STALE_PREDICTOR_IDENTITY")
        response = self.prediction_client.post(
            endpoint + ":predict", json={"instances": evaluation["holdout"]["x"]}
        )
        response.raise_for_status()
        output = response.json()
        if (
            output.get("model") != description
            or len(output["predictions"]) != len(evaluation["predictions"])
            or any(
                len(a) != 1 or not math.isfinite(a[0]) or abs(a[0] - b[0]) > 1e-12
                for a, b in zip(output["predictions"], evaluation["predictions"], strict=True)
            )
        ):
            raise ServingPending("SERVED_PREDICTIONS_MISMATCH")
        return {
            **description,
            "endpoint": endpoint,
            "isvc_uid": isvc["metadata"]["uid"],
            "deployment_uid": deployment["metadata"]["uid"],
            "deployment_generation": deployment["metadata"]["generation"],
            "service_uid": service["metadata"]["uid"],
            "image": self.config.image,
            "package_configmap": cm_name,
            "prediction_count": len(output["predictions"]),
            "prediction_digest": signature(output["predictions"]),
            "verified_at": now().isoformat(),
        }
