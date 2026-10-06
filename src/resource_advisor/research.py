"""Project-scoped operational views over real MLflow, KFP and Notebook APIs."""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx
from sqlalchemy import select

from .contracts import now
from .service import NotFound, Rejected
from .store import Conflict, jobs


class UpstreamUnavailable(RuntimeError):
    pass


class ResearchServices:
    def __init__(self, store, config, transport=None):
        self.store, self.config, self.transport = store, config, transport

    def settings(self, project, name):
        value = self.config.get("projects", {}).get(project, {}).get(name)
        if not value:
            raise Rejected(name + " is not configured for this project")
        return value

    def call(self, project, name, method, path, **kwargs):
        cfg = self.settings(project, name)
        headers = {}
        try:
            if cfg.get("token_file"):
                headers["Authorization"] = "Bearer " + Path(cfg["token_file"]).read_text().strip()
            with httpx.Client(
                base_url=cfg["url"].rstrip("/"),
                headers=headers,
                timeout=8,
                verify=cfg.get("ca_file", True),
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json() if response.content else {}
        except (httpx.HTTPError, ValueError, OSError):
            raise UpstreamUnavailable(
                name + " API unavailable; check service connectivity and permissions"
            ) from None

    def experiments(self, project):
        cfg = self.settings(project, "mlflow")
        items = [
            self.call(
                project,
                "mlflow",
                "GET",
                "/api/2.0/mlflow/experiments/get",
                params={"experiment_id": ref},
            )["experiment"]
            for ref in cfg["experiment_ids"]
        ]
        return {
            "items": [
                {k: e.get(k) for k in ("experiment_id", "name", "lifecycle_stage")} for e in items
            ],
            "ui_url": cfg.get("ui_url"),
        }

    def runs(self, project, experiment=None, page_token=None):
        cfg = self.settings(project, "mlflow")
        if experiment and experiment not in cfg["experiment_ids"]:
            raise NotFound("experiment not found")
        body = {
            "experiment_ids": [experiment] if experiment else cfg["experiment_ids"],
            "max_results": 50,
            "order_by": ["attributes.start_time DESC"],
        }
        if page_token:
            body["page_token"] = page_token
        result = self.call(project, "mlflow", "POST", "/api/2.0/mlflow/runs/search", json=body)
        return {
            "items": [
                self.run_view(r)
                for r in result.get("runs", [])
                if r["info"]["experiment_id"] in cfg["experiment_ids"]
            ],
            "next_page_token": result.get("next_page_token"),
        }

    @staticmethod
    def run_view(run):
        info, data = run["info"], run.get("data", {})

        def pairs(name):
            return {x["key"]: x["value"] for x in data.get(name, [])}

        tags = pairs("tags")
        return {
            "run_id": info["run_id"],
            "experiment_id": info["experiment_id"],
            "name": info.get("run_name") or tags.get("mlflow.runName", info["run_id"]),
            "status": info.get("status"),
            "start_time": info.get("start_time"),
            "end_time": info.get("end_time"),
            "metrics": pairs("metrics"),
            "params": pairs("params"),
            "tags": tags,
        }

    def run(self, project, ref):
        result = self.call(
            project, "mlflow", "GET", "/api/2.0/mlflow/runs/get", params={"run_id": ref}
        )["run"]
        if (
            result["info"]["experiment_id"]
            not in self.settings(project, "mlflow")["experiment_ids"]
        ):
            raise NotFound("run not found")
        return self.run_view(result)

    def artifacts(self, project, ref, path=""):
        self.run(project, ref)
        if path.startswith("/") or ".." in path.split("/"):
            raise Rejected("relative artifact path required")
        value = self.call(
            project,
            "mlflow",
            "GET",
            "/api/2.0/mlflow/artifacts/list",
            params={"run_id": ref, "path": path},
        )
        return {"items": value.get("files", [])}

    def note(self, project, ref, text):
        self.run(project, ref)
        self.call(
            project,
            "mlflow",
            "POST",
            "/api/2.0/mlflow/runs/set-tag",
            json={"run_id": ref, "key": "hairp.operator_note", "value": text},
        )
        return {"saved": True}

    def pipeline_runs(self, project, page_token=None):
        cfg = self.settings(project, "kubeflow")
        params = {"namespace": cfg["namespace"], "page_size": 50, "sort_by": "created_at desc"}
        if page_token:
            params["page_token"] = page_token
        result = self.call(project, "kubeflow", "GET", "/apis/v2beta1/runs", params=params)
        return {
            "items": [
                self.pipeline_view(project, r)
                for r in result.get("runs", [])
                if r.get("experiment_id") in cfg["experiment_ids"]
            ],
            "next_page_token": result.get("next_page_token"),
            "templates": [
                {"ref": k, "name": v["name"]} for k, v in cfg.get("templates", {}).items()
            ],
            "ui_url": cfg.get("ui_url"),
        }

    def pipeline_raw(self, project, ref):
        value = self.call(project, "kubeflow", "GET", "/apis/v2beta1/runs/" + quote(ref, safe=""))
        if value.get("experiment_id") not in self.settings(project, "kubeflow")["experiment_ids"]:
            raise NotFound("pipeline run not found")
        return value

    def pipeline_view(self, project, run):
        params = run.get("runtime_config", {}).get("parameters", {})
        linked = None
        if params.get("run_key"):
            with self.store.transaction() as conn:
                linked = conn.execute(
                    select(jobs.c.id).where(
                        jobs.c.project == project, jobs.c.idempotency_key == params["run_key"]
                    )
                ).scalar()
        tasks = [
            {
                k: t.get(k)
                for k in ("task_id", "display_name", "state", "start_time", "end_time", "error")
            }
            for t in run.get("run_details", {}).get("task_details", [])
        ]
        spec = run.get("pipeline_spec", {})
        spec = spec.get("pipeline_spec", spec)
        dag = spec.get("root", {}).get("dag", {}).get("tasks", {})
        return {
            **{
                k: run.get(k)
                for k in (
                    "run_id",
                    "display_name",
                    "state",
                    "created_at",
                    "scheduled_at",
                    "finished_at",
                    "error",
                )
            },
            "tasks": tasks,
            "graph": [
                {"name": name, "dependencies": task.get("dependentTasks", [])}
                for name, task in dag.items()
            ],
            "job_id": linked,
            "workload": params.get("workload"),
            "scheduling_profile": params.get("profile"),
        }

    def terminate(self, project, ref):
        run = self.pipeline_raw(project, ref)
        if run.get("state") not in {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED", "SKIPPED"}:
            self.call(
                project,
                "kubeflow",
                "POST",
                "/apis/v2beta1/runs/" + quote(ref, safe="") + ":terminate",
            )
        return {
            "requested": True,
            "message": "Termination requested; refresh for confirmed state. Compute owner lease handles launcher loss.",
        }

    def launch(self, service, project, template_ref, workload, profile, key):
        from .scheduling import SchedulingPlanRequest, compile_plan

        if not key or len(key) > 128:
            raise Rejected("Idempotency-Key required")
        cfg = self.settings(project, "kubeflow")
        template = cfg.get("templates", {}).get(template_ref)
        if not template:
            raise NotFound("pipeline template not found")
        ref = "kfp-" + hashlib.sha256((project + ":" + key).encode()).hexdigest()[:40]
        request = {"template": template_ref, "workload": workload, "profile": profile}
        with self.store.transaction() as conn:
            previous = self.store.get(conn, "pipeline_request", ref)
            if previous:
                if previous["project"] != project or previous["body"]["request"] != request:
                    raise Conflict("idempotency key reused with different pipeline request")
                receipt = self.store.get(conn, "pipeline_receipt", ref)
                if receipt:
                    return receipt["body"]
                raise Conflict(
                    "Pipeline submission is unresolved; inspect runs before making a new request"
                )
            plan = compile_plan(
                service,
                conn,
                project,
                SchedulingPlanRequest(profile_ref=profile, workload_ref=workload),
            )
            if not plan["accepted"]:
                raise Rejected("No compatible workload candidate")
            self.store.put(
                conn,
                "pipeline_request",
                ref,
                project,
                {"request": request, "created_at": now().isoformat()},
            )
        # Never automatically replay an uncertain remote create. The durable request
        # survives process/network failures and the compute run key remains stable.
        params = {
            **template["parameters"],
            "workload": workload,
            "profile": profile,
            "run_key": ref,
        }
        raw = self.call(
            project,
            "kubeflow",
            "POST",
            "/apis/v2beta1/runs",
            json={
                "display_name": ref,
                "experiment_id": cfg["experiment_ids"][0],
                "pipeline_spec": template["pipeline_spec"],
                "runtime_config": {"parameters": params},
                "service_account": template.get("service_account", "default-editor"),
            },
        )
        result = self.pipeline_view(project, raw)
        with self.store.transaction() as conn:
            self.store.put(conn, "pipeline_receipt", ref, project, result)
        return result

    def notebooks(self, project):
        cfg = self.settings(project, "notebooks")
        path = "/apis/kubeflow.org/v1/namespaces/" + quote(cfg["namespace"], safe="") + "/notebooks"
        result = self.call(project, "notebooks", "GET", path)
        items = []
        for obj in result.get("items", []):
            name = obj["metadata"]["name"]
            stopped = "kubeflow-resource-stopped" in obj["metadata"].get("annotations", {})
            status = obj.get("status", {})
            items.append(
                {
                    "name": name,
                    "namespace": cfg["namespace"],
                    "stopped": stopped,
                    "ready": status.get("readyReplicas", 0),
                    "conditions": status.get("conditions", []),
                    "resources": [
                        c.get("resources", {})
                        for c in obj["spec"]["template"]["spec"]["containers"]
                    ],
                    "url": cfg.get("ui_url", "").rstrip("/")
                    + "/notebook/"
                    + quote(cfg["namespace"], safe="")
                    + "/"
                    + quote(name, safe="")
                    + "/",
                }
            )
        return {"items": items}

    def notebook_action(self, project, name, action):
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name) or action not in {
            "start",
            "stop",
        }:
            raise Rejected("invalid notebook action")
        cfg = self.settings(project, "notebooks")
        path = (
            "/apis/kubeflow.org/v1/namespaces/"
            + quote(cfg["namespace"], safe="")
            + "/notebooks/"
            + name
        )
        current = self.call(project, "notebooks", "GET", path)
        self.call(
            project,
            "notebooks",
            "PATCH",
            path,
            headers={"Content-Type": "application/merge-patch+json"},
            json={
                "metadata": {
                    "resourceVersion": current["metadata"]["resourceVersion"],
                    "annotations": {
                        "kubeflow-resource-stopped": now().strftime("%Y-%m-%dT%H:%M:%SZ")
                        if action == "stop"
                        else None
                    },
                }
            },
        )
        return {"requested": action, "message": "Controller reconciliation pending"}

    def overview(self, project):
        sources = {
            "mlflow": self.experiments,
            "kubeflow": self.pipeline_runs,
            "notebooks": self.notebooks,
        }

        def fetch(item):
            name, callback = item
            if not self.config.get("projects", {}).get(project, {}).get(name):
                return name, {"status": "unconfigured", "items": []}
            try:
                return name, {"status": "connected", **callback(project)}
            except (UpstreamUnavailable, Rejected, NotFound):
                return name, {
                    "status": "unavailable",
                    "items": [],
                    "message": "API connection or authorization needs attention",
                }

        with ThreadPoolExecutor(max_workers=3) as pool:
            return {"observed_at": now().isoformat(), **dict(pool.map(fetch, sources.items()))}
