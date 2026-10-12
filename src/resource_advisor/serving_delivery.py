"""Verified registry package to KServe; successful receipts retain historic evidence."""

import base64
import hashlib
import json

import httpx

from .contracts import signature
from .kserve_deployment import ServingPending
from .model_predictor import PACKAGE_FILES, verified_package
from .model_serving import history


class ServingDelivery:
    def __init__(self, store, registry, adapter, *, job_routes):
        self.store, self.registry, self.adapter, self.job_routes = (
            store,
            registry,
            adapter,
            job_routes,
        )

    def deliver_one(self):
        event = self.store.claim("model_deployment", job_routes=self.job_routes)
        if not event:
            return False
        try:
            self.advance(event)
            self.store.finish(event)
        except (ValueError, KeyError, TypeError, OverflowError, httpx.HTTPError) as exc:
            self.store.finish(
                event, str(exc) if isinstance(exc, ServingPending) else type(exc).__name__
            )
        return True

    def advance(self, event):
        ref = event["body"]["ref"]
        with self.store.transaction() as conn:
            intent = self.store.get(conn, "model_deployment", ref)
            project, body = intent["project"], intent["body"]
            if history(self.store, conn, project, body["service_ref"])[0]["ref"] != ref:
                self.store.put(conn, "model_deployment_superseded", ref, project, {"ref": ref})
                return
            if self.store.get(conn, "model_deployment_ready", ref):
                return
            row = self.store.job(conn, event["body"]["job_id"])
            policy_ref = self.store.get(conn, "model_lifecycle", body["lifecycle_ref"])["body"][
                "policy_ref"
            ]
            policy = self.store.get(conn, "model_lifecycle_policy", policy_ref)["body"]
            evaluation = self.store.get(conn, "model_evaluation", body["lifecycle_ref"])["body"]
            previous = history(self.store, conn, project, body["service_ref"])[1:]
            receipts = [self.store.get(conn, "model_deployment_ready", x["ref"]) for x in previous]
            old_uid = next((r["body"]["isvc_uid"] for r in receipts if r), None)
        if project not in self.adapter.config.project_namespaces:
            raise ServingPending("SERVING_PROJECT_NOT_CONFIGURED")
        registered = body["registration"]
        run_id, proxy = self.registry.owned_run(project, row)
        model = self.registry.get("registered-models/get", name=registered["model_name"])[
            "registered_model"
        ]
        tags = {t["key"]: t["value"] for t in model.get("tags", [])}
        if (
            tags.get("resource_advisor.project") != project
            or model["name"] != registered["model_name"]
        ):
            raise ServingPending("REGISTRY_OWNERSHIP_MISMATCH")
        expected = {
            "name": registered["model_name"],
            "run_id": run_id,
            "source": f"runs:/{run_id}/{registered['artifact_path']}",
            "tags": {
                "resource_advisor.project": project,
                "resource_advisor.lifecycle_id": body["lifecycle_ref"],
                "resource_advisor.evaluation_digest": registered["evaluation_digest"],
                "resource_advisor.package_digest": registered["model_package_digest"],
                "resource_advisor.output_checkpoint_digest": registered["output_checkpoint_digest"],
            },
        }
        version = self.registry.find_version(
            registered["model_name"], body["lifecycle_ref"], expected
        )
        if (
            not version
            or version["version"] != registered["model_version"]
            or version["status"] != "READY"
            or run_id != registered["run_id"]
            or not evaluation["passed"]
            or signature(evaluation) != registered["evaluation_digest"]
        ):
            raise ServingPending("REGISTERED_VERSION_MISMATCH")
        if set(registered["files"]) != PACKAGE_FILES:
            raise ServingPending("MODEL_PACKAGE_FILE_MISMATCH")
        files = {}
        for name, digest in registered["files"].items():
            with self.registry.client.stream(
                "GET", f"{proxy}/{registered['artifact_path']}/{name}"
            ) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 128 * 1024:
                        raise ServingPending("MODEL_PACKAGE_SIZE_EXCEEDED")
                if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                    raise ServingPending("MODEL_PACKAGE_INTEGRITY_MISMATCH")
                files[name] = base64.b64encode(data).decode()
        name = "ra-model-" + signature([project, body["service_ref"]])[7:39]
        package = {
            "files": files,
            "description": {
                "service_name": name,
                "deployment_ref": ref,
                "model_uri": registered["model_uri"],
                "model_package_digest": registered["model_package_digest"],
                "output_checkpoint_digest": registered["output_checkpoint_digest"],
            },
        }
        verified_package(package)
        lineage = json.loads(base64.b64decode(files["lineage.json"]))
        if lineage != {"policy": policy, "evaluation": evaluation}:
            raise ServingPending("MODEL_LINEAGE_MISMATCH")
        # Recheck after registry reads. Kubernetes resourceVersion/revision fences
        # also prevent an older in-flight update from replacing a newer write.
        with self.store.transaction() as conn:
            if history(self.store, conn, project, body["service_ref"])[0]["ref"] != ref:
                self.store.put(conn, "model_deployment_superseded", ref, project, {"ref": ref})
                return
        isvc, cm_name = self.adapter.apply(project, intent, package)
        receipt = self.adapter.verify(
            project,
            intent,
            package,
            isvc,
            cm_name,
            {**evaluation, "holdout": policy["holdout"]},
            old_uid,
        )
        with self.store.transaction() as conn:
            if history(self.store, conn, project, body["service_ref"])[0]["ref"] == ref:
                self.store.put(conn, "model_deployment_ready", ref, project, receipt)
            else:
                self.store.put(conn, "model_deployment_superseded", ref, project, {"ref": ref})
