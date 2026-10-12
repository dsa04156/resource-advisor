"""Verified JSON pyfunc packages and fail-closed external version reconciliation."""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .artifacts import ArtifactError
from .contracts import signature
from .model_lifecycle import LifecyclePending


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def model_files(checkpoint, evaluation, policy, run_id):
    """Fixed loader source, no pickle or user-provided executable model code."""
    requirements = ["mlflow==3.16.1"]
    manifest = {
        "mlflow_version": "3.16.1",
        "model_uuid": signature(evaluation)[7:39],
        "run_id": run_id,
        "utc_time_created": evaluation["evaluated_at"],
        "flavors": {
            "python_function": {
                "loader_module": "ra_linear_model",
                "code": "code",
                "data": "data/checkpoint.json",
                "python_version": "3.11",
                "env": {"conda": "conda.yaml", "virtualenv": "python_env.yaml"},
            }
        },
        "signature": {
            "inputs": json.dumps(
                [{"type": "tensor", "tensor-spec": {"dtype": "float64", "shape": [-1, 2]}}]
            ),
            "outputs": json.dumps(
                [{"type": "tensor", "tensor-spec": {"dtype": "float64", "shape": [-1, 1]}}]
            ),
        },
    }
    return {
        "MLmodel": encoded(manifest),
        "code/ra_linear_model.py": Path(__file__).with_name("linear_model.py").read_bytes(),
        "data/checkpoint.json": encoded(checkpoint),
        "lineage.json": encoded(
            {"policy": policy.model_dump(mode="json"), "evaluation": evaluation}
        ),
        "requirements.txt": ("\n".join(requirements) + "\n").encode(),
        "conda.yaml": encoded(
            {
                "name": "ra-linear-model",
                "channels": ["conda-forge"],
                "dependencies": ["python=3.11", "pip", {"pip": requirements}],
            }
        ),
        "python_env.yaml": encoded(
            {
                "python": "3.11",
                "build_dependencies": ["pip"],
                "dependencies": ["-r requirements.txt"],
            }
        ),
    }


class MLflowModelRegistry:
    def __init__(self, store, delivery):
        self.store, self.delivery, self.client = store, delivery, delivery.client

    def get(self, path, **params):
        response = self.client.get("/api/2.0/mlflow/" + path, params=params)
        response.raise_for_status()
        return response.json()

    def owned_run(self, project, job):
        with self.store.transaction() as conn:
            tracking = self.store.get(conn, "tracking", job["body"]["attempt_id"])
        if not tracking or tracking["project"] != project:
            raise LifecyclePending("MLFLOW_RUN_NOT_READY")
        link = tracking["body"]
        run = self.get("runs/get", run_id=link["run_id"])["run"]
        tags = {t["key"]: t["value"] for t in run["data"]["tags"]}
        if (
            run["info"]["experiment_id"] != self.delivery.experiments[project]
            or run["info"]["run_id"] != link["run_id"]
            or run["info"]["status"] != "FINISHED"
            or tags.get("project") != project
            or tags.get("resource_advisor.attempt_id") != job["body"]["attempt_id"]
            or tags.get("resource_advisor.job_id") != job["id"]
        ):
            raise ArtifactError("external training run ownership/status mismatch")
        uri = urlparse(run["info"]["artifact_uri"])
        parts = uri.path.lstrip("/").split("/")
        if (
            uri.scheme != "mlflow-artifacts"
            or uri.netloc
            or uri.query
            or uri.fragment
            or any(not re.fullmatch(r"[a-zA-Z0-9_-]+", p) for p in parts)
            or not re.fullmatch(r"[a-f0-9]{32}", link["run_id"])
        ):
            raise ArtifactError("same-server MLflow artifact proxy URI required")
        return link["run_id"], "/api/2.0/mlflow-artifacts/artifacts/" + "/".join(parts)

    def verify_file(self, path, data):
        def read_back():
            with self.client.stream("GET", path) as response:
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 128 * 1024:
                        raise ArtifactError("model file exceeds bounded package contract")
                return bytes(content)

        existing = read_back()
        if existing is None:
            response = self.client.put(path, content=data)
            response.raise_for_status()
            existing = read_back()
        if existing != data:
            raise ArtifactError("model file integrity mismatch; refusing overwrite")

    def owned_model(self, name, project):
        response = self.client.get("/api/2.0/mlflow/registered-models/get", params={"name": name})
        if (
            response.status_code == 404
            and response.json().get("error_code") == "RESOURCE_DOES_NOT_EXIST"
        ):
            self.delivery.post(
                "registered-models/create",
                {
                    "name": name,
                    "tags": [{"key": "resource_advisor.project", "value": project}],
                },
            )
            model = self.get("registered-models/get", name=name)["registered_model"]
        else:
            response.raise_for_status()
            model = response.json()["registered_model"]
        tags = {t["key"]: t["value"] for t in model.get("tags", [])}
        if model["name"] != name or tags.get("resource_advisor.project") != project:
            raise ArtifactError("registered model belongs to a different owner")

    def find_version(self, name, ref, expected):
        found, token = [], None
        for _ in range(100):
            page = self.get(
                "model-versions/search",
                filter=f"name = '{name}'",
                max_results=100,
                **({"page_token": token} if token else {}),
            )
            for version in page.get("model_versions", []):
                tags = {t["key"]: t["value"] for t in version.get("tags", [])}
                if tags.get("resource_advisor.lifecycle_id") == ref:
                    if any(tags.get(k) != v for k, v in expected["tags"].items()) or any(
                        version.get(k) != expected[k] for k in ("name", "source", "run_id")
                    ):
                        raise ArtifactError("model version lineage mismatch")
                    found.append(version)
            token = page.get("next_page_token")
            if not token:
                break
        else:
            raise LifecyclePending("REGISTRY_SEARCH_LIMIT")
        if len(found) > 1:
            raise ArtifactError("duplicate model versions require reconciliation")
        return found[0] if found else None

    def register(self, ref, job, policy, evaluation, checkpoint, event):
        project = job["project"]
        run_id, proxy = self.owned_run(project, job)
        files = model_files(checkpoint, evaluation, policy, run_id)
        hashes = {
            name: "sha256:" + hashlib.sha256(data).hexdigest() for name, data in files.items()
        }
        package_digest = signature(hashes)
        artifact_path = f"resource-advisor-models/{ref}/{package_digest[7:]}"
        for name, data in files.items():
            self.verify_file(f"{proxy}/{artifact_path}/{name}", data)
        self.delivery.post(
            "runs/log-batch",
            {
                "run_id": run_id,
                "metrics": [
                    {
                        "key": "lifecycle.holdout_mse",
                        "value": evaluation["mse"],
                        "timestamp": int(
                            datetime.fromisoformat(evaluation["evaluated_at"]).timestamp() * 1000
                        ),
                        "step": 0,
                    }
                ],
                "params": [],
                "tags": [
                    {"key": "resource_advisor.lifecycle_id", "value": ref},
                    {"key": "lifecycle.evaluation_digest", "value": signature(evaluation)},
                ],
            },
        )
        self.owned_model(policy.model_name, project)
        expected = {
            "name": policy.model_name,
            "source": f"runs:/{run_id}/{artifact_path}",
            "run_id": run_id,
            "tags": {
                "resource_advisor.project": project,
                "resource_advisor.lifecycle_id": ref,
                "resource_advisor.evaluation_digest": signature(evaluation),
                "resource_advisor.package_digest": package_digest,
                "resource_advisor.output_checkpoint_digest": evaluation["output_checkpoint_digest"],
            },
        }
        version = self.find_version(policy.model_name, ref, expected)
        if version is None:
            with self.store.transaction() as conn:
                intent = self.store.get(conn, "model_registry_intent", ref)
                if intent:
                    raise LifecyclePending("REGISTRY_RECONCILIATION_REQUIRED")
                # Only the lease that first commits this unique intent may create.
                # A later lease only searches; it never retries an ambiguous POST.
                self.store.put(
                    conn,
                    "model_registry_intent",
                    ref,
                    project,
                    {**expected, "creator_lease": event["lease_token"]},
                )
            self.delivery.post(
                "model-versions/create",
                {
                    **expected,
                    "tags": [{"key": k, "value": v} for k, v in expected["tags"].items()],
                },
            )
            version = self.find_version(policy.model_name, ref, expected)
        if not version or version["status"] != "READY":
            raise LifecyclePending("MODEL_VERSION_NOT_READY")
        if not str(version["version"]).isdigit():
            raise ArtifactError("invalid model version")
        return {
            "lifecycle_ref": ref,
            "training_job_id": job["id"],
            "training_attempt_id": job["body"]["attempt_id"],
            "run_id": run_id,
            "model_name": policy.model_name,
            "model_version": str(version["version"]),
            "model_uri": f"models:/{policy.model_name}/{version['version']}",
            "evaluation_digest": signature(evaluation),
            "model_package_digest": package_digest,
            "output_checkpoint_digest": evaluation["output_checkpoint_digest"],
            "artifact_path": artifact_path,
            "files": hashes,
        }
